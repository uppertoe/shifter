from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from shifter import pay, repos
from shifter.auth import current_user
from shifter.config import Settings, get_settings
from shifter.main import get_db, templates
from shifter.money import parse_dollars
from shifter.time_utils import parse_local_input, to_local_input


def _htmx_swap_with_oob(request: Request, conn, settings: Settings, user: str,
                          card_html: str = "") -> HTMLResponse:
    """Standard HTMX response for a dashboard-mutating action: empty (or
    given) main-target HTML, plus OOB-swap fragments for the dashboard's
    summary sections so totals/counts re-render in place."""
    # Late import: dashboard imports from main.get_db; avoids any cycle.
    from shifter.routes.dashboard import render_oob_refresh
    oob = render_oob_refresh(conn, settings, user)
    return HTMLResponse(card_html + oob)

router = APIRouter(prefix="/shifts")


def _none_if_blank(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s or None


def _parse_optional_dollars(s: str | None) -> int | None:
    s = _none_if_blank(s)
    if s is None:
        return None
    return parse_dollars(s)


def _shift_view(conn, settings: Settings, shift_row) -> dict:
    """Build a render-ready dict for a shift row."""
    cs = pay.compute_shift(conn, shift_row)
    nanny = repos.get_nanny(conn, shift_row["nanny_id"])
    expenses = repos.list_expenses(conn, shift_row["id"])
    expenses_total = sum(int(e["amount_cents"]) for e in expenses)
    unpaid_expenses = [e for e in expenses if e["paid_on"] is None]
    return {
        "shift": shift_row,
        "nanny": nanny,
        "computed": cs,
        "expenses": expenses,
        "expenses_total_cents": expenses_total,
        "unpaid_expenses_count": len(unpaid_expenses),
        "unpaid_expenses_total_cents": sum(int(e["amount_cents"]) for e in unpaid_expenses),
        "tz": settings.zoneinfo,
    }


# --- list page ---------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def list_page(
    request: Request,
    nanny_id: str | None = None,
    paid: str | None = None,        # 'yes' | 'no' | None
    confirmed: str | None = None,   # 'yes' | 'no' | None
    open_only: int = 0,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    paid_filter = {"yes": True, "no": False}.get(paid)
    confirmed_filter = {"yes": True, "no": False}.get(confirmed)
    nanny_id_int = int(nanny_id) if nanny_id else None
    rows = repos.list_shifts(
        conn,
        nanny_id=nanny_id_int,
        paid=paid_filter,
        confirmed=confirmed_filter,
        open_only=bool(open_only),
        limit=200,
    )
    nannies = repos.list_nannies(conn, include_inactive=True)
    views = [_shift_view(conn, settings, r) for r in rows]
    return templates.TemplateResponse(
        request,
        "shifts/index.html",
        {
            "user": user,
            "shifts": views,
            "nannies": nannies,
            "filter_nanny_id": nanny_id_int,
            "filter_paid": paid,
            "filter_confirmed": confirmed,
            "filter_open": bool(open_only),
            "tz": settings.zoneinfo,
            "today_iso": date.today().isoformat(),
        },
    )


# --- create / edit pages -----------------------------------------------------

def _form_context(conn, settings: Settings, *, shift=None, error: str | None = None) -> dict:
    nannies = repos.list_nannies(conn, include_inactive=False)
    expenses = repos.list_expenses(conn, shift["id"]) if shift else []
    expenses_total_cents = sum(int(e["amount_cents"]) for e in expenses)
    if shift:
        start_local = to_local_input(shift["start_time"], settings.zoneinfo)
        end_local = to_local_input(shift["end_time"], settings.zoneinfo) if shift["end_time"] else ""
    else:
        # Default to today 7am
        now_local = datetime.now(settings.zoneinfo).replace(hour=7, minute=0, second=0, microsecond=0)
        start_local = now_local.strftime("%Y-%m-%dT%H:%M")
        end_local = ""
    return {
        "shift": shift,
        "shift_id": shift["id"] if shift else None,
        "nannies": nannies,
        "expenses": expenses,
        "expenses_total_cents": expenses_total_cents,
        "start_local": start_local,
        "end_local": end_local,
        "error": error,
        "tz": settings.zoneinfo,
    }


@router.get("/new", response_class=HTMLResponse)
def new_form(
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    ctx = _form_context(conn, settings)
    return templates.TemplateResponse(request, "shifts/edit.html", {"user": user, **ctx})


@router.post("", response_class=HTMLResponse)
def create(
    request: Request,
    nanny_id: int = Form(...),
    start_local: str = Form(...),
    end_local: str = Form(""),
    rate_override: str = Form(""),
    flat_rate: str = Form(""),
    notes: str = Form(""),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    try:
        start = parse_local_input(start_local, settings.zoneinfo).isoformat()
        end_dt = parse_local_input(end_local, settings.zoneinfo) if end_local.strip() else None
        end = end_dt.isoformat() if end_dt else None
        if end_dt and parse_local_input(start_local, settings.zoneinfo) > end_dt:
            raise ValueError("end time must be after start time")
        rate_override_cents = _parse_optional_dollars(rate_override)
        flat_rate_cents = _parse_optional_dollars(flat_rate)
    except ValueError as e:
        ctx = _form_context(conn, settings, error=str(e))
        return templates.TemplateResponse(
            request, "shifts/edit.html", {"user": user, **ctx}, status_code=400
        )

    # All shifts start unconfirmed: the explicit confirm step is the human
    # signoff that the times (whether typed in or auto-filled by HA) are
    # right. Manual creation is no exception.
    repos.create_shift(
        conn,
        nanny_id=nanny_id,
        start_time=start,
        end_time=end,
        rate_override_cents=rate_override_cents,
        flat_rate_cents=flat_rate_cents,
        notes=_none_if_blank(notes),
        source="manual",
        confirmed=False,
        created_by=user,
    )
    return RedirectResponse("/shifts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/confirm-batch", response_class=HTMLResponse)
def confirm_batch(
    request: Request,
    shift_ids: list[int] = Form(...),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    """Confirm a list of shifts in one click — bound to the explicit IDs
    rendered on the dashboard so older pending shifts off-screen aren't
    swept up unintentionally. Defined before the ``/{shift_id}`` routes so
    the literal ``confirm-batch`` path doesn't get parsed as an int id."""
    repos.confirm_shifts(conn, shift_ids, updated_by=user)
    if request.headers.get("HX-Request"):
        return _htmx_swap_with_oob(request, conn, settings, user)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{shift_id}/edit", response_class=HTMLResponse)
def edit_form(
    shift_id: int,
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    shift = repos.get_shift(conn, shift_id)
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    ctx = _form_context(conn, settings, shift=shift)
    return templates.TemplateResponse(request, "shifts/edit.html", {"user": user, **ctx})


@router.post("/{shift_id}", response_class=HTMLResponse)
def update(
    shift_id: int,
    request: Request,
    nanny_id: int = Form(...),
    start_local: str = Form(...),
    end_local: str = Form(""),
    rate_override: str = Form(""),
    flat_rate: str = Form(""),
    notes: str = Form(""),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    shift = repos.get_shift(conn, shift_id)
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        start = parse_local_input(start_local, settings.zoneinfo).isoformat()
        end_dt = parse_local_input(end_local, settings.zoneinfo) if end_local.strip() else None
        end = end_dt.isoformat() if end_dt else None
        if end_dt and parse_local_input(start_local, settings.zoneinfo) > end_dt:
            raise ValueError("end time must be after start time")
        rate_override_cents = _parse_optional_dollars(rate_override)
        flat_rate_cents = _parse_optional_dollars(flat_rate)
    except ValueError as e:
        ctx = _form_context(conn, settings, shift=shift, error=str(e))
        return templates.TemplateResponse(
            request, "shifts/edit.html", {"user": user, **ctx}, status_code=400
        )

    repos.update_shift(
        conn, shift_id,
        nanny_id=nanny_id,
        start_time=start,
        end_time=end,
        rate_override_cents=rate_override_cents,
        flat_rate_cents=flat_rate_cents,
        notes=_none_if_blank(notes),
        updated_by=user,
    )
    return RedirectResponse(f"/shifts/{shift_id}/edit", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{shift_id}/delete")
def delete(
    shift_id: int,
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    repos.delete_shift(conn, shift_id)
    if request.headers.get("HX-Request"):
        return _htmx_swap_with_oob(request, conn, settings, user)
    return RedirectResponse("/shifts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{shift_id}/close", response_class=HTMLResponse)
def close(
    shift_id: int,
    request: Request,
    end_time: str | None = Form(default=None),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    if end_time:
        end_dt = parse_local_input(end_time, settings.zoneinfo)
    else:
        end_dt = datetime.now(settings.zoneinfo)
    shift = repos.get_shift(conn, shift_id)
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such shift")
    start_dt = datetime.fromisoformat(shift["start_time"])
    if end_dt <= start_dt:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                              "end time must be after start time")
    repos.close_shift(conn, shift_id, end_dt.isoformat(), updated_by=user)
    if request.headers.get("HX-Request"):
        return _htmx_swap_with_oob(request, conn, settings, user)
    return RedirectResponse("/shifts", status_code=status.HTTP_303_SEE_OTHER)


# --- inline editor (used by dashboard cards) --------------------------------

@router.get("/{shift_id}/inline-editor", response_class=HTMLResponse)
def inline_editor(
    shift_id: int,
    request: Request,
    row_id: str,
    confirm_on_save: int = 0,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    shift = repos.get_shift(conn, shift_id)
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    expenses = repos.list_expenses(conn, shift_id)
    expenses_total = sum(int(e["amount_cents"]) for e in expenses)
    start_local = to_local_input(shift["start_time"], settings.zoneinfo)
    end_local = to_local_input(shift["end_time"], settings.zoneinfo) if shift["end_time"] else ""
    return templates.TemplateResponse(
        request,
        "dashboard/_inline_editor.html",
        {
            "user": user,
            "shift": shift,
            "start_local": start_local,
            "end_local": end_local,
            "expenses": expenses,
            "expenses_total_cents": expenses_total,
            "confirm_on_save": bool(confirm_on_save),
            "row_id": row_id,
        },
    )


@router.post("/{shift_id}/inline-save", response_class=HTMLResponse)
def inline_save(
    shift_id: int,
    request: Request,
    start_local: str = Form(...),
    end_local: str = Form(""),
    notes: str = Form(""),
    confirm: str = Form("0"),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    shift = repos.get_shift(conn, shift_id)
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        start_dt = parse_local_input(start_local, settings.zoneinfo)
        end_dt = parse_local_input(end_local, settings.zoneinfo) if end_local.strip() else None
        if end_dt and end_dt <= start_dt:
            raise ValueError("end time must be after start time")
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    repos.update_shift(
        conn, shift_id,
        nanny_id=shift["nanny_id"],
        start_time=start_dt.isoformat(),
        end_time=end_dt.isoformat() if end_dt else None,
        rate_override_cents=shift["rate_override_cents"],
        flat_rate_cents=shift["flat_rate_cents"],
        notes=_none_if_blank(notes),
        updated_by=user,
    )
    if confirm == "1":
        repos.confirm_shift(conn, shift_id, updated_by=user)
    if request.headers.get("HX-Request"):
        return _htmx_swap_with_oob(request, conn, settings, user)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


def _safe_next(path: str, fallback: str) -> str:
    """Only allow redirects to internal paths. Reject anything that doesn't
    start with a single slash so an attacker can't send a payment form to
    "//evil.com/" and bounce the user off-site."""
    if path and path.startswith("/") and not path.startswith("//"):
        return path
    return fallback


@router.post("/{shift_id}/pay")
def pay_shift(
    shift_id: int,
    request: Request,
    paid_on: str = Form(...),
    paid_note: str = Form(""),
    include_expenses: int = Form(0),
    next: str = Form(""),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    """Mark a single shift paid. By default the form sends include_expenses=1
    so the shift's unpaid expenses are settled in the same payment; the user
    can untick the checkbox to settle the shift on its own. An unchecked
    checkbox simply isn't submitted, hence the default of 0.

    The optional ``next`` form field lets callers (e.g. /nannies/X/unpaid)
    return the user to their original page after the redirect."""
    shift = repos.get_shift(conn, shift_id)
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        paid_date = date.fromisoformat(paid_on)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    repos.mark_shifts_paid(
        conn, [shift_id], paid_date,
        paid_note=paid_note.strip() or None, updated_by=user,
    )
    if include_expenses:
        unpaid_expense_ids = [
            e["id"] for e in repos.list_expenses(conn, shift_id)
            if e["paid_on"] is None
        ]
        if unpaid_expense_ids:
            repos.mark_expenses_paid(conn, unpaid_expense_ids, paid_date)
    return RedirectResponse(
        _safe_next(next, "/shifts"), status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{shift_id}/confirm", response_class=HTMLResponse)
def confirm(
    shift_id: int,
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    repos.confirm_shift(conn, shift_id, updated_by=user)
    # HTMX submission from the dashboard pending-review card: return empty
    # body so the card is swapped out in place, plus OOB fragments so totals
    # and pending-shifts counts re-render. Plain browser submit (no HTMX)
    # falls back to the legacy redirect.
    if request.headers.get("HX-Request"):
        return _htmx_swap_with_oob(request, conn, settings, user)
    return RedirectResponse("/shifts", status_code=status.HTTP_303_SEE_OTHER)


# --- expenses (HTMX-managed within shift edit page) --------------------------

@router.post("/{shift_id}/expenses", response_class=HTMLResponse)
def add_expense(
    shift_id: int,
    request: Request,
    description: str = Form(...),
    amount: str = Form(...),
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    shift = repos.get_shift(conn, shift_id)
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        cents = parse_dollars(amount)
        desc = description.strip()
        if not desc:
            raise ValueError("description required")
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    repos.create_expense(conn, shift_id=shift_id, amount_cents=cents, description=desc)
    expenses = repos.list_expenses(conn, shift_id)
    total = sum(int(e["amount_cents"]) for e in expenses)
    return templates.TemplateResponse(
        request,
        "shifts/_expenses.html",
        {"shift_id": shift_id, "expenses": expenses, "expenses_total_cents": total},
    )


@router.post("/{shift_id}/expenses/{expense_id}/delete", response_class=HTMLResponse)
def delete_expense(
    shift_id: int,
    expense_id: int,
    request: Request,
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    repos.delete_expense(conn, expense_id)
    expenses = repos.list_expenses(conn, shift_id)
    total = sum(int(e["amount_cents"]) for e in expenses)
    return templates.TemplateResponse(
        request,
        "shifts/_expenses.html",
        {"shift_id": shift_id, "expenses": expenses, "expenses_total_cents": total},
    )
