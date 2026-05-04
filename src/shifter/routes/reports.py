from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from shifter import reports as rep
from shifter import repos
from shifter.auth import current_user
from shifter.config import Settings, get_settings
from shifter.main import get_db, templates

router = APIRouter(prefix="/reports")


def _resolve_period(
    preset: str | None,
    from_str: str | None,
    to_str: str | None,
    fy_start_month: int,
    today: date,
) -> tuple[date, date, str]:
    """Return (start_inclusive, end_exclusive, label). Falls through preset → custom."""
    if preset == "this_fy" or preset is None:
        s, e = rep.fy_bounds(today, fy_start_month)
        return s, e, f"This {rep.fy_label(s, fy_start_month)}"
    if preset == "last_fy":
        s_now, _ = rep.fy_bounds(today, fy_start_month)
        # Subtract one day to land in the previous FY
        s, e = rep.fy_bounds(s_now - timedelta(days=1), fy_start_month)
        return s, e, f"Last {rep.fy_label(s, fy_start_month)}"
    if preset == "this_cy":
        s, e = date(today.year, 1, 1), date(today.year + 1, 1, 1)
        return s, e, f"CY {today.year}"
    if preset == "last_30":
        s = today - timedelta(days=30)
        return s, today + timedelta(days=1), "Last 30 days"
    # custom
    try:
        s = date.fromisoformat(from_str) if from_str else today.replace(day=1)
        e = date.fromisoformat(to_str) + timedelta(days=1) if to_str else today + timedelta(days=1)
    except ValueError:
        s, e = rep.fy_bounds(today, fy_start_month)
    return s, e, f"{s.isoformat()} to {(e - timedelta(days=1)).isoformat()}"


@router.get("", response_class=HTMLResponse)
def view(
    request: Request,
    preset: str | None = None,
    nanny_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    today = date.today()
    start, end, period_label = _resolve_period(
        preset, date_from, date_to, settings.fy_start_month, today
    )
    totals = rep.per_nanny_totals(conn, start=start, end=end)
    if nanny_id is not None:
        totals = [t for t in totals if t.nanny_id == nanny_id]
    nannies = repos.list_nannies(conn, include_inactive=True)
    grand = {
        "shift_count": sum(t.shift_count for t in totals),
        "hours": sum(float(t.hours) for t in totals),
        "gross_pay_cents": sum(t.gross_pay_cents for t in totals),
        "expenses_total_cents": sum(t.expenses_total_cents for t in totals),
        "owing_cents": sum(t.owing_cents for t in totals),
        "total_paid_cents": sum(t.total_paid_cents for t in totals),
    }
    return templates.TemplateResponse(
        request,
        "reports/index.html",
        {
            "user": user,
            "totals": totals,
            "grand": grand,
            "nannies": nannies,
            "filter_nanny_id": nanny_id,
            "preset": preset or "this_fy",
            "date_from": start.isoformat(),
            "date_to": (end - timedelta(days=1)).isoformat(),
            "period_label": period_label,
        },
    )


@router.get("/shifts.csv", response_class=PlainTextResponse)
def shifts_csv(
    preset: str | None = None,
    nanny_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    today = date.today()
    start, end, _ = _resolve_period(
        preset, date_from, date_to, settings.fy_start_month, today
    )
    body = rep.shifts_csv(conn, start=start, end=end, nanny_id=nanny_id, tz=settings.zoneinfo)
    fname = f"shifts_{start.isoformat()}_{(end - timedelta(days=1)).isoformat()}.csv"
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/expenses.csv", response_class=PlainTextResponse)
def expenses_csv(
    preset: str | None = None,
    nanny_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    today = date.today()
    start, end, _ = _resolve_period(
        preset, date_from, date_to, settings.fy_start_month, today
    )
    body = rep.expenses_csv(conn, start=start, end=end, nanny_id=nanny_id)
    fname = f"expenses_{start.isoformat()}_{(end - timedelta(days=1)).isoformat()}.csv"
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
