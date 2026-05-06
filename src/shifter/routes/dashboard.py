from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse

from shifter import ha, pay, repos, screenshots
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

    open_shifts = [s for s in repos.list_shifts(conn, open_only=True)
                   if _is_visible(s)]
    open_views = []
    for s in open_shifts:
        v = _shift_brief(conn, s, now=now, with_shots=True)
        v["is_stale"] = ha.is_open_shift_stale(conn, s, as_of=now, settings=settings)
        open_views.append(v)

    # Pending review: only this-week-or-newer on the dashboard for at-a-glance
    # focus. Older unconfirmed shifts get a footer link.
    all_pending = [s for s in repos.list_shifts(conn, confirmed=False)
                   if _is_visible(s)]
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
