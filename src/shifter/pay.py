"""Pay computation. Pure functions where possible; DB-backed lookups isolated.

Money is stored as integer cents throughout. Hours use Decimal to avoid float drift
in pay totals. Datetimes must be timezone-aware.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

SECONDS_PER_HOUR = Decimal(3600)


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"datetime {s!r} is naive; expected tz-aware ISO 8601")
    return dt


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def shift_hours(
    start: datetime,
    end: datetime | None,
    *,
    now: datetime | None = None,
) -> Decimal:
    """Duration of a shift in hours. Open shifts run up to `now` (default: utcnow)."""
    if start.tzinfo is None:
        raise ValueError("start must be tz-aware")
    if end is not None and end.tzinfo is None:
        raise ValueError("end must be tz-aware")
    end_eff = end if end is not None else (now or datetime.now(timezone.utc))
    seconds = Decimal((end_eff - start).total_seconds())
    if seconds < 0:
        return Decimal(0)
    return seconds / SECONDS_PER_HOUR


def shift_pay_cents(
    *,
    flat_rate_cents: int | None,
    rate_override_cents: int | None,
    fallback_rate_cents: int | None,
    start: datetime,
    end: datetime | None,
    now: datetime | None = None,
) -> int:
    """Compute pay for a single shift. Returns 0 if no applicable rate.

    Precedence: flat_rate > rate_override > fallback (effective rate from history).
    """
    if flat_rate_cents is not None:
        return int(flat_rate_cents)

    rate = rate_override_cents if rate_override_cents is not None else fallback_rate_cents
    if rate is None:
        return 0

    hours = shift_hours(start, end, now=now)
    cents = (hours * Decimal(rate)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(cents)


def effective_rate_cents(
    conn: sqlite3.Connection, nanny_id: int, on_date: date
) -> int | None:
    """Most recent pay_rate for `nanny_id` with effective_from <= on_date."""
    row = conn.execute(
        "SELECT rate_cents FROM pay_rates "
        "WHERE nanny_id = ? AND effective_from <= ? "
        "ORDER BY effective_from DESC LIMIT 1",
        (nanny_id, on_date.isoformat()),
    ).fetchone()
    return int(row["rate_cents"]) if row else None


@dataclass(frozen=True)
class ComputedShift:
    shift_id: int
    nanny_id: int
    start: datetime
    end: datetime | None
    hours: Decimal
    pay_cents: int
    is_open: bool


def compute_shift(
    conn: sqlite3.Connection,
    shift_row: sqlite3.Row,
    *,
    now: datetime | None = None,
) -> ComputedShift:
    start = _parse_dt(shift_row["start_time"])
    end = _parse_dt(shift_row["end_time"]) if shift_row["end_time"] else None
    fallback = (
        effective_rate_cents(conn, shift_row["nanny_id"], start.date())
        if shift_row["flat_rate_cents"] is None and shift_row["rate_override_cents"] is None
        else None
    )
    pay = shift_pay_cents(
        flat_rate_cents=shift_row["flat_rate_cents"],
        rate_override_cents=shift_row["rate_override_cents"],
        fallback_rate_cents=fallback,
        start=start,
        end=end,
        now=now,
    )
    return ComputedShift(
        shift_id=shift_row["id"],
        nanny_id=shift_row["nanny_id"],
        start=start,
        end=end,
        hours=shift_hours(start, end, now=now),
        pay_cents=pay,
        is_open=end is None,
    )


def expense_total_cents(
    conn: sqlite3.Connection, shift_ids: Iterable[int], *, only_unpaid: bool = False
) -> int:
    ids = list(shift_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    sql = f"SELECT COALESCE(SUM(amount_cents), 0) AS t FROM expenses WHERE shift_id IN ({placeholders})"
    if only_unpaid:
        sql += " AND paid_on IS NULL"
    row = conn.execute(sql, ids).fetchone()
    return int(row["t"])


@dataclass
class UnpaidLine:
    shift: ComputedShift


@dataclass
class UnpaidExpense:
    expense_id: int
    shift_id: int
    shift_start: datetime
    description: str
    amount_cents: int
    pending_review: bool


@dataclass
class UnpaidSummary:
    nanny_id: int
    shifts: list[ComputedShift]
    expenses: list[UnpaidExpense]
    shifts_subtotal_cents: int
    expenses_subtotal_cents: int

    @property
    def grand_total_cents(self) -> int:
        return self.shifts_subtotal_cents + self.expenses_subtotal_cents


def unpaid_summary(
    conn: sqlite3.Connection,
    nanny_id: int,
    *,
    now: datetime | None = None,
    include_open: bool = False,
) -> UnpaidSummary:
    """All unpaid shifts and unpaid expenses for a nanny. Open shifts excluded by default."""
    sql = (
        "SELECT * FROM shifts "
        "WHERE nanny_id = ? AND paid_on IS NULL"
    )
    if not include_open:
        sql += " AND end_time IS NOT NULL"
    sql += " ORDER BY start_time"
    shift_rows = conn.execute(sql, (nanny_id,)).fetchall()
    computed = [compute_shift(conn, r, now=now) for r in shift_rows]
    shifts_total = sum(s.pay_cents for s in computed)

    exp_rows = conn.execute(
        "SELECT e.id, e.shift_id, e.amount_cents, e.description, e.pending_review, "
        "       s.start_time AS shift_start "
        "FROM expenses e JOIN shifts s ON s.id = e.shift_id "
        "WHERE s.nanny_id = ? AND e.paid_on IS NULL "
        "ORDER BY s.start_time, e.id",
        (nanny_id,),
    ).fetchall()
    expenses = [
        UnpaidExpense(
            expense_id=r["id"],
            shift_id=r["shift_id"],
            shift_start=_parse_dt(r["shift_start"]),
            description=r["description"],
            amount_cents=int(r["amount_cents"]),
            pending_review=bool(r["pending_review"]),
        )
        for r in exp_rows
    ]
    expenses_total = sum(e.amount_cents for e in expenses)

    return UnpaidSummary(
        nanny_id=nanny_id,
        shifts=computed,
        expenses=expenses,
        shifts_subtotal_cents=shifts_total,
        expenses_subtotal_cents=expenses_total,
    )
