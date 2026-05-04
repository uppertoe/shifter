from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from shifter import pay, repos
from shifter.auth import current_user
from shifter.config import Settings, get_settings
from shifter.main import get_db, templates

router = APIRouter()


def _shift_brief(conn, shift_row, *, now: datetime | None = None):
    """Lightweight view of a shift for dashboard listings."""
    cs = pay.compute_shift(conn, shift_row, now=now)
    nanny = repos.get_nanny(conn, shift_row["nanny_id"])
    return {"shift": shift_row, "nanny": nanny, "computed": cs}


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    tz = settings.zoneinfo
    now = datetime.now(tz)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())  # Monday

    open_shifts = repos.list_shifts(conn, open_only=True)
    open_views = [_shift_brief(conn, s, now=now) for s in open_shifts]

    pending_shifts = repos.list_shifts(conn, confirmed=False)
    pending_views = [_shift_brief(conn, s, now=now) for s in pending_shifts]

    unresolved_count = conn.execute(
        "SELECT COUNT(*) AS c FROM ha_events WHERE resolution = 'unresolved'"
    ).fetchone()["c"]

    week_shifts = repos.list_shifts(
        conn,
        start_date=week_start,
        end_date=today + timedelta(days=1),
    )
    week_views = [_shift_brief(conn, s, now=now) for s in week_shifts]
    week_hours = sum(float(v["computed"].hours) for v in week_views)
    week_pay = sum(v["computed"].pay_cents for v in week_views)

    today_views = [v for v in week_views if v["computed"].start.date() == today]
    today_hours = sum(float(v["computed"].hours) for v in today_views)
    today_pay = sum(v["computed"].pay_cents for v in today_views)

    nannies = repos.list_nannies(conn, include_inactive=False)
    nanny_summaries = []
    for n in nannies:
        s = pay.unpaid_summary(conn, n["id"], now=now)
        nanny_summaries.append({"nanny": n, "summary": s})

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
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
            "pending_shifts": pending_views,
            "unresolved_count": unresolved_count,
            "nanny_summaries": nanny_summaries,
        },
    )
