from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse

from shifter import ha, pay, repos, schedule, screenshots
from shifter.auth import current_user
from shifter.config import Settings, get_settings
from shifter.main import get_db, templates

router = APIRouter()


def _shift_brief(conn, shift_row, *, now: datetime | None = None, with_shots: bool = False):
    """Lightweight view of a shift for dashboard listings."""
    cs = pay.compute_shift(conn, shift_row, now=now)
    nanny = repos.get_nanny(conn, shift_row["nanny_id"])
    out = {"shift": shift_row, "nanny": nanny, "computed": cs}
    if with_shots:
        out["shots"] = screenshots.shots_for_shift(conn, shift_row["id"])
    return out


# --- screenshot serving ------------------------------------------------------

@router.get("/screenshots/{rel_path:path}", include_in_schema=False)
def serve_screenshot(
    rel_path: str,
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    """Serve a screenshot file by its DB-stored relative path. Auth-gated and
    sandboxed to settings.screenshot_dir to block path traversal."""
    base = settings.screenshot_dir.resolve()
    try:
        target = (base / rel_path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return FileResponse(target)


def _build_context(conn, settings: Settings, user: str) -> dict:
    """Compute the data the dashboard needs. Reused for the full page render
    and for the OOB refresh fragment that mutation endpoints append."""
    tz = settings.zoneinfo
    now = datetime.now(tz)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())  # Monday

    # Hidden nannies (show_on_dashboard=0) are filtered out everywhere on the
    # dashboard — open shifts, pending review, stats, owed summaries — so the
    # toggle is a single visual eject. /shifts and /nannies still list them.
    visible_nannies = repos.list_nannies(
        conn, include_inactive=True, dashboard_only=True
    )
    visible_nanny_ids = {n["id"] for n in visible_nannies}

    def _is_visible(shift) -> bool:
        return shift["nanny_id"] in visible_nanny_ids

    expected_today_all = schedule.pending_for_date(conn, today, tz=tz)
    expected_today = []
    for slot in expected_today_all:
        if slot.nanny_id not in visible_nanny_ids:
            continue
        start_dt, end_dt = schedule.slot_window(slot, tz)
        auto_open_at = schedule.auto_open_due_at(slot, tz, settings)
        expected_today.append({
            "slot": slot,
            "nanny": repos.get_nanny(conn, slot.nanny_id),
            "start_dt": start_dt,
            "end_dt": end_dt,
            "is_due": start_dt <= now,
            "is_over": end_dt <= now,
            "auto_open_at": auto_open_at,
            "minutes_to_start": int((start_dt - now).total_seconds() // 60),
            "minutes_to_auto_open": int((auto_open_at - now).total_seconds() // 60),
        })

    open_shifts = [s for s in repos.list_shifts(conn, open_only=True)
                   if _is_visible(s)]
    open_views = []
    for s in open_shifts:
        v = _shift_brief(conn, s, now=now, with_shots=True)
        v["is_stale"] = ha.is_open_shift_stale(conn, s, as_of=now, settings=settings)
        open_views.append(v)

    # Pending review: only this-week-or-newer on the dashboard for at-a-glance
    # focus. Older unconfirmed shifts get a footer link to /review. Open
    # shifts are excluded — they can't be signed off until they have an end
    # time, and they already have their own section above.
    all_pending = [s for s in repos.list_shifts(conn, confirmed=False)
                   if _is_visible(s) and s["end_time"]]
    pending_recent = [s for s in all_pending
                      if datetime.fromisoformat(s["start_time"]).date() >= week_start]
    pending_older_count = len(all_pending) - len(pending_recent)
    pending_views = [_shift_brief(conn, s, now=now, with_shots=True)
                     for s in pending_recent]

    unresolved_count = conn.execute(
        "SELECT COUNT(*) AS c FROM ha_events WHERE resolution = 'unresolved'"
    ).fetchone()["c"]

    week_shifts = [
        s for s in repos.list_shifts(
            conn, start_date=week_start, end_date=today + timedelta(days=1),
        ) if _is_visible(s)
    ]
    week_views = [_shift_brief(conn, s, now=now) for s in week_shifts]
    week_hours = sum(float(v["computed"].hours) for v in week_views)
    week_pay = sum(v["computed"].pay_cents for v in week_views)

    today_views = [v for v in week_views if v["computed"].start.date() == today]
    today_hours = sum(float(v["computed"].hours) for v in today_views)
    today_pay = sum(v["computed"].pay_cents for v in today_views)

    nanny_summaries = []
    for n in visible_nannies:
        if not n["active"]:
            continue
        s = pay.unpaid_summary(conn, n["id"], now=now)
        nanny_summaries.append({"nanny": n, "summary": s})

    return {
        "user": user,
        "tz": tz,
        "today": today,
        "today_iso": today.isoformat(),
        "today_hours": today_hours,
        "today_pay_cents": today_pay,
        "week_start": week_start,
        "week_hours": week_hours,
        "week_pay_cents": week_pay,
        "expected_today": expected_today,
        "open_shifts": open_views,
        "now_local_input": now.strftime("%Y-%m-%dT%H:%M"),
        "pending_shifts": pending_views,
        "pending_older_count": pending_older_count,
        "frigate_base_url": settings.frigate_base_url,
        "unresolved_count": unresolved_count,
        "nanny_summaries": nanny_summaries,
    }


def render_oob_refresh(conn, settings: Settings, user: str) -> str:
    """Render the dynamic dashboard sections wrapped for HTMX out-of-band
    swap. Mutation endpoints append this to their HTMX response so totals
    and counts stay in sync after a card vanishes."""
    ctx = _build_context(conn, settings, user)
    ctx["oob"] = True
    return templates.env.get_template("dashboard/_oob_refresh.html").render(ctx)


def render_pending_card(conn, settings: Settings, shift_id: int, row_id: str) -> str:
    """Re-render one unconfirmed shift as a review card (used when an inline
    editor is cancelled somewhere other than the dashboard, where there is no
    OOB refresh to restore the card)."""
    shift = repos.get_shift(conn, shift_id)
    if shift is None:
        return ""
    now = datetime.now(settings.zoneinfo)
    return templates.env.get_template("dashboard/_pending_card.html").render({
        "v": _shift_brief(conn, shift, now=now, with_shots=True),
        "row_id": row_id,
        "tz": settings.zoneinfo,
        "frigate_base_url": settings.frigate_base_url,
    })


@router.get("/review", response_class=HTMLResponse)
def review(
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    """Every unconfirmed shift, regardless of age or dashboard visibility.
    The dashboard only shows this week's; older ones used to be reachable only
    through the shifts table, which had no Confirm button and no snapshots."""
    now = datetime.now(settings.zoneinfo)
    unconfirmed = repos.list_shifts(conn, confirmed=False)
    closed = [s for s in unconfirmed if s["end_time"]]
    open_count = len(unconfirmed) - len(closed)
    views = [_shift_brief(conn, s, now=now, with_shots=True) for s in closed]
    return templates.TemplateResponse(
        request, "review.html", {
            "user": user,
            "tz": settings.zoneinfo,
            "pending_shifts": views,
            "open_unconfirmed_count": open_count,
            "frigate_base_url": settings.frigate_base_url,
        },
    )


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    return templates.TemplateResponse(
        request, "dashboard.html", _build_context(conn, settings, user),
    )


# --- expected-shift actions (dashboard surface) ------------------------------
#
# Endpoints used by the "Expected today" section. Each returns the dashboard
# OOB refresh fragment so the section list, stats and any cascades stay in
# sync after the row's status changes. The row itself disappears from the
# Expected section as soon as a matching shift exists (post-Open) or the slot
# is cancelled.

def _get_expected_or_404(conn, expected_id: int):
    row = conn.execute(
        "SELECT * FROM expected_shifts WHERE id = ?", (expected_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such expected shift")
    return row


def _expected_slot(conn, expected_id: int) -> schedule.ExpectedSlot:
    r = conn.execute(
        "SELECT e.*, n.name AS nanny_name FROM expected_shifts e"
        " JOIN nannies n ON n.id = e.nanny_id WHERE e.id = ?",
        (expected_id,),
    ).fetchone()
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such expected shift")
    return schedule.ExpectedSlot(
        expected_id=r["id"],
        nanny_id=r["nanny_id"],
        nanny_name=r["nanny_name"],
        date=date.fromisoformat(r["date"]),
        start_time=r["start_time"],
        end_time=r["end_time"],
        source=r["source"],
        cancelled=bool(r["cancelled"]),
        pattern_id=r["pattern_id"],
    )


@router.post("/dashboard/expected/{expected_id}/open", response_class=HTMLResponse)
def expected_open(
    expected_id: int,
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    slot = _expected_slot(conn, expected_id)
    # Always open at the scheduled start — same time the auto-opener would
    # use. The row only appears when no matching shift exists, so this can't
    # collide with an already-open shift. If the nanny actually arrived
    # earlier/later, the human edits the shift in the Open shifts section.
    scheduled_start, _ = schedule.slot_window(slot, settings.zoneinfo)
    repos.create_shift(
        conn,
        nanny_id=slot.nanny_id,
        start_time=scheduled_start.isoformat(),
        end_time=None,
        rate_override_cents=None,
        flat_rate_cents=None,
        notes=None,
        source="manual",
        confirmed=False,
        created_by=user,
    )
    return HTMLResponse(render_oob_refresh(conn, settings, user))


@router.post("/dashboard/expected/{expected_id}/cancel", response_class=HTMLResponse)
def expected_cancel(
    expected_id: int,
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    _get_expected_or_404(conn, expected_id)
    schedule.cancel_expected(conn, expected_id)
    return HTMLResponse(render_oob_refresh(conn, settings, user))


@router.get("/dashboard/expected/{expected_id}/edit", response_class=HTMLResponse)
def expected_edit(
    expected_id: int,
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    slot = _expected_slot(conn, expected_id)
    return templates.TemplateResponse(
        request,
        "dashboard/_expected_editor.html",
        {"slot": slot, "nanny": repos.get_nanny(conn, slot.nanny_id), "tz": settings.zoneinfo},
    )


@router.get("/dashboard/expected/{expected_id}/cancel-edit", response_class=HTMLResponse)
def expected_cancel_edit(
    expected_id: int,
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    """Revert an opened editor back to the read-only row."""
    return HTMLResponse(render_oob_refresh(conn, settings, user))


@router.post("/dashboard/expected/{expected_id}/update", response_class=HTMLResponse)
def expected_update(
    expected_id: int,
    request: Request,
    start_time: str = Form(...),
    end_time: str = Form(...),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    _get_expected_or_404(conn, expected_id)
    schedule.update_expected_times(
        conn, expected_id, start_time=start_time, end_time=end_time,
    )
    return HTMLResponse(render_oob_refresh(conn, settings, user))
