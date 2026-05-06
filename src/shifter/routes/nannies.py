from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from shifter import pay, repos
from shifter.auth import current_user
from shifter.config import Settings, get_settings
from shifter.main import get_db, templates
from shifter.money import parse_dollars

router = APIRouter(prefix="/nannies")


def _row_ctx(conn, nanny_row):
    return {
        "nanny": nanny_row,
        "current_rate": repos.current_rate(conn, nanny_row["id"]),
        "rates": repos.list_rates(conn, nanny_row["id"]),
    }


def _row_response(request, user, conn, nanny_row):
    return templates.TemplateResponse(
        request,
        "nannies/_row.html",
        {"user": user, **_row_ctx(conn, nanny_row)},
    )


@router.get("", response_class=HTMLResponse)
def list_page(request: Request, conn=Depends(get_db), user: str = Depends(current_user)):
    nannies = repos.list_nannies(conn, include_inactive=True)
    rows = [_row_ctx(conn, n) for n in nannies]
    return templates.TemplateResponse(
        request,
        "nannies/index.html",
        {"user": user, "rows": rows, "today": date.today().isoformat()},
    )


@router.post("", response_class=HTMLResponse)
def create(
    request: Request,
    name: str = Form(...),
    rate: str = Form(...),
    effective_from: str = Form(...),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    name = name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name required")
    try:
        rate_cents = parse_dollars(rate)
        eff = date.fromisoformat(effective_from)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    nanny_id = repos.create_nanny(conn, name)
    repos.add_rate(conn, nanny_id, rate_cents, eff)

    nanny = repos.get_nanny(conn, nanny_id)
    assert nanny is not None
    return _row_response(request, user, conn, nanny)


@router.post("/{nanny_id}/rename", response_class=HTMLResponse)
def rename(
    nanny_id: int,
    request: Request,
    name: str = Form(...),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    name = name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name required")
    repos.rename_nanny(conn, nanny_id, name)
    nanny = repos.get_nanny(conn, nanny_id)
    if nanny is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _row_response(request, user, conn, nanny)


@router.post("/{nanny_id}/payment", response_class=HTMLResponse)
def update_payment(
    nanny_id: int,
    request: Request,
    payment_notes: str = Form(""),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    repos.set_payment_notes(conn, nanny_id, payment_notes.strip() or None)
    nanny = repos.get_nanny(conn, nanny_id)
    if nanny is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _row_response(request, user, conn, nanny)


@router.post("/{nanny_id}/active", response_class=HTMLResponse)
def toggle_active(
    nanny_id: int,
    request: Request,
    active: int = Form(...),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    repos.set_nanny_active(conn, nanny_id, bool(active))
    nanny = repos.get_nanny(conn, nanny_id)
    if nanny is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _row_response(request, user, conn, nanny)


@router.post("/{nanny_id}/visibility", response_class=HTMLResponse)
def toggle_visibility(
    nanny_id: int,
    request: Request,
    show: int = Form(...),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    """Hide or show a nanny on the dashboard. Independent of active state —
    hidden nannies are still listed under /nannies and /shifts; they just
    don't clutter the today/this-week summaries."""
    repos.set_nanny_dashboard_visibility(conn, nanny_id, bool(show))
    nanny = repos.get_nanny(conn, nanny_id)
    if nanny is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _row_response(request, user, conn, nanny)


@router.post("/{nanny_id}/rates", response_class=HTMLResponse)
def add_rate(
    nanny_id: int,
    request: Request,
    rate: str = Form(...),
    effective_from: str = Form(...),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    try:
        rate_cents = parse_dollars(rate)
        eff = date.fromisoformat(effective_from)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    nanny = repos.get_nanny(conn, nanny_id)
    if nanny is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    try:
        repos.add_rate(conn, nanny_id, rate_cents, eff)
    except Exception as e:
        # Most likely the UNIQUE(nanny_id, effective_from) constraint
        raise HTTPException(status.HTTP_409_CONFLICT, f"could not add rate: {e}")

    return _row_response(request, user, conn, nanny)


@router.get("/{nanny_id}/unpaid", response_class=HTMLResponse)
def unpaid_view(
    nanny_id: int,
    request: Request,
    conn=Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: str = Depends(current_user),
):
    nanny = repos.get_nanny(conn, nanny_id)
    if nanny is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    summary = pay.unpaid_summary(conn, nanny_id)
    return templates.TemplateResponse(
        request,
        "nannies/unpaid.html",
        {
            "user": user,
            "nanny": nanny,
            "summary": summary,
            "tz": settings.zoneinfo,
            "today_iso": date.today().isoformat(),
        },
    )


@router.post("/{nanny_id}/pay-all")
def pay_all(
    nanny_id: int,
    paid_on: str = Form(...),
    paid_note: str = Form(""),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    nanny = repos.get_nanny(conn, nanny_id)
    if nanny is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        paid_date = date.fromisoformat(paid_on)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    summary = pay.unpaid_summary(conn, nanny_id)
    shift_ids = [s.shift_id for s in summary.shifts]
    expense_ids = [e.expense_id for e in summary.expenses]

    if shift_ids:
        repos.mark_shifts_paid(
            conn, shift_ids, paid_date,
            paid_note=paid_note.strip() or None, updated_by=user,
        )
    if expense_ids:
        repos.mark_expenses_paid(conn, expense_ids, paid_date)

    from fastapi.responses import RedirectResponse  # local import: only this route uses it
    return RedirectResponse(f"/nannies/{nanny_id}/unpaid", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{nanny_id}/expenses/pay-all")
def pay_all_expenses(
    nanny_id: int,
    paid_on: str = Form(...),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    """Bulk-pay every unpaid expense for this nanny — leaves shifts untouched."""
    nanny = repos.get_nanny(conn, nanny_id)
    if nanny is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        paid_date = date.fromisoformat(paid_on)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    summary = pay.unpaid_summary(conn, nanny_id)
    expense_ids = [e.expense_id for e in summary.expenses]
    if expense_ids:
        repos.mark_expenses_paid(conn, expense_ids, paid_date)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/nannies/{nanny_id}/unpaid",
                              status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{nanny_id}/expenses/{expense_id}/pay")
def pay_expense(
    nanny_id: int,
    expense_id: int,
    paid_on: str = Form(...),
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    """Mark a single expense paid."""
    expense = repos.get_expense(conn, expense_id)
    if expense is None or expense["shift_id"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Sanity check: the expense actually belongs to this nanny via its shift.
    shift = repos.get_shift(conn, expense["shift_id"])
    if shift is None or shift["nanny_id"] != nanny_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        paid_date = date.fromisoformat(paid_on)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    repos.mark_expenses_paid(conn, [expense_id], paid_date)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/nannies/{nanny_id}/unpaid",
                              status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{nanny_id}/rates/{rate_id}/delete", response_class=HTMLResponse)
def delete_rate(
    nanny_id: int,
    rate_id: int,
    request: Request,
    conn=Depends(get_db),
    user: str = Depends(current_user),
):
    repos.delete_rate(conn, rate_id)
    nanny = repos.get_nanny(conn, nanny_id)
    if nanny is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _row_response(request, user, conn, nanny)
