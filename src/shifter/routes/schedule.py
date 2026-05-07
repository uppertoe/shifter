from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from shifter import repos, schedule
from shifter.auth import current_user
from shifter.config import Settings, get_settings
from shifter.main import get_db, templates

router = APIRouter(prefix="/schedule")


DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_month(month: str | None, today: date) -> date:
    """'YYYY-MM' → first day of that month. None → first of current month."""
    if not month:
        return today.replace(day=1)
    try:
        y, m = month.split("-")
        return date(int(y), int(m), 1)
    except (ValueError, AttributeError):
        return today.replace(day=1)


def _month_grid(month_start: date) -> list[list[date]]:
    """Return weeks (Mon-first) covering the entire month, padded with adjacent days."""
    cal = calendar.Calendar(firstweekday=0)  # Monday
    return [list(week) for week in cal.monthdatescalendar(month_start.year, month_start.month)]


@router.get("", response_class=HTMLResponse)
def index(
    request: Request,
    month: str | None = None,
    nanny_id: int | None = None,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    today = datetime.now(settings.zoneinfo).date()
    # Always run a cheap projection refresh on view (idempotent).
    schedule.materialize(conn)

    month_start = _parse_month(month, today)
    if month_start.month == 12:
        month_end = date(month_start.year + 1, 1, 1)
    else:
        month_end = date(month_start.year, month_start.month + 1, 1)

    weeks = _month_grid(month_start)
    grid_start = weeks[0][0]
    grid_end = weeks[-1][-1] + timedelta(days=1)
    expected = schedule.expected_in_range(conn, start=grid_start, end=grid_end, include_cancelled=True)

    by_date: dict[date, list[schedule.ExpectedSlot]] = defaultdict(list)
    for slot in expected:
        by_date[slot.date].append(slot)

    nannies = repos.list_nannies(conn, include_inactive=False)
    selected_nanny = next((n for n in nannies if n["id"] == nanny_id), nannies[0] if nannies else None)

    patterns = schedule.list_patterns(conn)

    prev_month = (month_start - timedelta(days=1)).replace(day=1)
    next_month = month_end

    return templates.TemplateResponse(
        request,
        "schedule/index.html",
        {
            "user": user,
            "today": today,
            "month_start": month_start,
            "month_label": month_start.strftime("%B %Y"),
            "weeks": weeks,
            "by_date": by_date,
            "nannies": nannies,
            "selected_nanny": selected_nanny,
            "patterns": patterns,
            "dow_names": DOW_NAMES,
            "prev_month_str": prev_month.strftime("%Y-%m"),
            "next_month_str": next_month.strftime("%Y-%m"),
            "default_start": "07:00",
            "default_end": "18:00",
        },
    )


# --- pattern CRUD ------------------------------------------------------------

@router.post("/patterns")
def create_pattern(
    nanny_id: int = Form(...),
    day_of_week: int = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    active_from: str = Form(...),
    active_until: str = Form(""),
    notes: str = Form(""),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    try:
        af = date.fromisoformat(active_from)
        au = date.fromisoformat(active_until) if active_until.strip() else None
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    schedule.create_pattern(
        conn,
        nanny_id=nanny_id,
        day_of_week=day_of_week,
        start_time=start_time,
        end_time=end_time,
        active_from=af,
        active_until=au,
        notes=notes.strip() or None,
    )
    schedule.materialize(conn)
    return RedirectResponse("/schedule", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/patterns/{pattern_id}/delete")
def delete_pattern(
    pattern_id: int,
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    schedule.delete_pattern(conn, pattern_id)
    return RedirectResponse("/schedule", status_code=status.HTTP_303_SEE_OTHER)


# --- expected_shifts CRUD (called by clicking days on the calendar) ----------

@router.post("/expected", response_class=HTMLResponse)
def add_expected(
    request: Request,
    nanny_id: int = Form(...),
    on_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    month: str = Form(""),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    try:
        d = date.fromisoformat(on_date)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    schedule.add_one_off(
        conn, nanny_id=nanny_id, on_date=d,
        start_time=start_time, end_time=end_time,
    )
    return _day_cell_response(request, conn, settings, user, d, nanny_id)


@router.post("/expected/{expected_id}/cancel", response_class=HTMLResponse)
def cancel_expected(
    expected_id: int,
    request: Request,
    nanny_id: int = Form(...),
    on_date: str = Form(...),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    try:
        d = date.fromisoformat(on_date)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    schedule.cancel_expected(conn, expected_id)
    return _day_cell_response(request, conn, settings, user, d, nanny_id)


@router.post("/expected/{expected_id}/uncancel", response_class=HTMLResponse)
def uncancel_expected(
    expected_id: int,
    request: Request,
    nanny_id: int = Form(...),
    on_date: str = Form(...),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    try:
        d = date.fromisoformat(on_date)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    schedule.uncancel_expected(conn, expected_id)
    return _day_cell_response(request, conn, settings, user, d, nanny_id)


@router.post("/expected/{expected_id}/delete", response_class=HTMLResponse)
def delete_expected(
    expected_id: int,
    request: Request,
    nanny_id: int = Form(...),
    on_date: str = Form(...),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    try:
        d = date.fromisoformat(on_date)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    schedule.delete_expected(conn, expected_id)
    return _day_cell_response(request, conn, settings, user, d, nanny_id)


def _day_cell_response(request, conn, settings, user, d: date, nanny_id: int):
    """Return just the updated day-cell partial for HTMX swap.

    materialize() runs first so that deleting a pattern-sourced expected
    shift instantly re-projects it as active — that's what makes the chip
    click cycle (active → cancelled → restored) work for recurring slots.
    Manual one-offs aren't affected (no pattern to project from)."""
    schedule.materialize(conn)
    today = datetime.now(settings.zoneinfo).date()
    slots = schedule.expected_on_date(conn, d, include_cancelled=True)
    nannies = repos.list_nannies(conn, include_inactive=False)
    selected_nanny = next((n for n in nannies if n["id"] == nanny_id), None)
    return templates.TemplateResponse(
        request,
        "schedule/_day_cell.html",
        {
            "day": d,
            "in_month": True,  # hint not needed when swapping in place
            "today": today,
            "slots": slots,
            "selected_nanny": selected_nanny,
            "default_start": "07:00",
            "default_end": "18:00",
        },
    )
