"""Reporting aggregations + CSV serialisation."""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from shifter import pay, repos
from shifter.time_utils import format_dt


def fy_bounds(d: date, fy_start_month: int) -> tuple[date, date]:
    """Return (start, end_exclusive) of the financial year containing `d`."""
    start_year = d.year if d.month >= fy_start_month else d.year - 1
    start = date(start_year, fy_start_month, 1)
    if fy_start_month == 1:
        end = date(start_year + 1, 1, 1)
    else:
        end = date(start_year + 1, fy_start_month, 1)
    return start, end


def fy_label(start: date, fy_start_month: int) -> str:
    """e.g. 'FY 2025–26' for AU FY starting July 2025."""
    if fy_start_month == 1:
        return f"CY {start.year}"
    end_year = start.year + 1
    return f"FY {start.year}–{str(end_year)[-2:]}"


@dataclass
class NannyTotals:
    nanny_id: int
    nanny_name: str
    shift_count: int
    hours: Decimal
    gross_pay_cents: int
    paid_shift_cents: int
    unpaid_shift_cents: int
    expenses_total_cents: int
    paid_expenses_cents: int
    unpaid_expenses_cents: int

    @property
    def owing_cents(self) -> int:
        return self.unpaid_shift_cents + self.unpaid_expenses_cents

    @property
    def total_paid_cents(self) -> int:
        return self.paid_shift_cents + self.paid_expenses_cents


def per_nanny_totals(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
) -> list[NannyTotals]:
    """One row per nanny who has at least one shift starting within [start, end)."""
    nannies = repos.list_nannies(conn, include_inactive=True)
    out: list[NannyTotals] = []
    for n in nannies:
        shifts = repos.list_shifts(
            conn, nanny_id=n["id"], start_date=start, end_date=end
        )
        if not shifts:
            continue
        hours = Decimal(0)
        gross = 0
        paid_shift = 0
        unpaid_shift = 0
        shift_ids = []
        for row in shifts:
            cs = pay.compute_shift(conn, row)
            hours += cs.hours
            gross += cs.pay_cents
            shift_ids.append(row["id"])
            if row["paid_on"]:
                paid_shift += (
                    int(row["paid_amount_cents"])
                    if row["paid_amount_cents"] is not None
                    else cs.pay_cents
                )
            else:
                unpaid_shift += cs.pay_cents

        # expenses on those shifts
        if shift_ids:
            placeholders = ",".join("?" * len(shift_ids))
            exp_rows = conn.execute(
                f"SELECT amount_cents, paid_on FROM expenses WHERE shift_id IN ({placeholders})",
                shift_ids,
            ).fetchall()
        else:
            exp_rows = []
        exp_total = sum(int(e["amount_cents"]) for e in exp_rows)
        exp_paid = sum(int(e["amount_cents"]) for e in exp_rows if e["paid_on"])
        exp_unpaid = exp_total - exp_paid

        out.append(
            NannyTotals(
                nanny_id=n["id"],
                nanny_name=n["name"],
                shift_count=len(shifts),
                hours=hours,
                gross_pay_cents=gross,
                paid_shift_cents=paid_shift,
                unpaid_shift_cents=unpaid_shift,
                expenses_total_cents=exp_total,
                paid_expenses_cents=exp_paid,
                unpaid_expenses_cents=exp_unpaid,
            )
        )
    return out


def shifts_csv(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    nanny_id: int | None,
    tz: ZoneInfo,
) -> str:
    rows = repos.list_shifts(
        conn, nanny_id=nanny_id, start_date=start, end_date=end
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "shift_id", "nanny", "start_local", "end_local", "hours",
        "rate_cents_per_hour_used", "rate_override_cents", "flat_rate_cents",
        "computed_pay_cents", "paid_on", "paid_amount_cents", "notes",
        "source", "confirmed",
    ])
    for r in rows:
        cs = pay.compute_shift(conn, r)
        nanny = repos.get_nanny(conn, r["nanny_id"])
        # Determine the rate that was used
        rate_used: int | None = None
        if r["flat_rate_cents"] is None:
            if r["rate_override_cents"] is not None:
                rate_used = int(r["rate_override_cents"])
            else:
                rt = pay.effective_rate_cents(conn, r["nanny_id"], cs.start.date())
                rate_used = rt
        w.writerow([
            r["id"],
            nanny["name"] if nanny else "",
            format_dt(r["start_time"], tz),
            format_dt(r["end_time"], tz) if r["end_time"] else "",
            f"{cs.hours:.2f}",
            rate_used if rate_used is not None else "",
            r["rate_override_cents"] if r["rate_override_cents"] is not None else "",
            r["flat_rate_cents"] if r["flat_rate_cents"] is not None else "",
            cs.pay_cents,
            r["paid_on"] or "",
            r["paid_amount_cents"] if r["paid_amount_cents"] is not None else "",
            (r["notes"] or "").replace("\n", " "),
            r["source"],
            "yes" if r["confirmed"] else "no",
        ])
    return buf.getvalue()


def expenses_csv(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    nanny_id: int | None,
) -> str:
    sql = (
        "SELECT e.id, e.shift_id, e.amount_cents, e.description, e.paid_on, e.pending_review,"
        "       s.start_time, s.nanny_id, n.name AS nanny_name "
        "FROM expenses e "
        "JOIN shifts s ON s.id = e.shift_id "
        "JOIN nannies n ON n.id = s.nanny_id "
        "WHERE s.start_time >= ? AND s.start_time < ?"
    )
    params: list = [start.isoformat(), end.isoformat()]
    if nanny_id is not None:
        sql += " AND s.nanny_id = ?"
        params.append(nanny_id)
    sql += " ORDER BY s.start_time, e.id"
    rows = conn.execute(sql, params).fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "expense_id", "shift_id", "shift_date", "nanny",
        "description", "amount_cents", "paid_on", "pending_review",
    ])
    for r in rows:
        # shift_date as YYYY-MM-DD (first 10 chars of stored ISO)
        shift_date = (r["start_time"] or "")[:10]
        w.writerow([
            r["id"], r["shift_id"], shift_date, r["nanny_name"],
            r["description"], r["amount_cents"], r["paid_on"] or "",
            "yes" if r["pending_review"] else "no",
        ])
    return buf.getvalue()
