"""Thin data-access helpers. Each function is one query; no implicit transactions.

Keeps SQL out of routes/templates and gives a place for the test suite to mock if it
ever needs to. Functions either return ``sqlite3.Row`` (or list thereof) or simple
typed values — no ORM models.
"""

from __future__ import annotations

import sqlite3
from datetime import date


# --- nannies -----------------------------------------------------------------

def list_nannies(
    conn: sqlite3.Connection,
    *,
    include_inactive: bool = False,
    dashboard_only: bool = False,
) -> list[sqlite3.Row]:
    where: list[str] = []
    if not include_inactive:
        where.append("active = 1")
    if dashboard_only:
        where.append("show_on_dashboard = 1")
    sql = "SELECT * FROM nannies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY active DESC, name"
    return conn.execute(sql).fetchall()


def get_nanny(conn: sqlite3.Connection, nanny_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM nannies WHERE id = ?", (nanny_id,)).fetchone()


def create_nanny(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute("INSERT INTO nannies (name) VALUES (?)", (name,))
    assert cur.lastrowid is not None
    return cur.lastrowid


def rename_nanny(conn: sqlite3.Connection, nanny_id: int, name: str) -> None:
    conn.execute("UPDATE nannies SET name = ? WHERE id = ?", (name, nanny_id))


def set_nanny_active(conn: sqlite3.Connection, nanny_id: int, active: bool) -> None:
    conn.execute("UPDATE nannies SET active = ? WHERE id = ?", (1 if active else 0, nanny_id))


def set_nanny_dashboard_visibility(
    conn: sqlite3.Connection, nanny_id: int, show: bool
) -> None:
    conn.execute(
        "UPDATE nannies SET show_on_dashboard = ? WHERE id = ?",
        (1 if show else 0, nanny_id),
    )


def set_payment_notes(conn: sqlite3.Connection, nanny_id: int, notes: str | None) -> None:
    conn.execute("UPDATE nannies SET payment_notes = ? WHERE id = ?", (notes, nanny_id))


# --- pay rates ---------------------------------------------------------------

def list_rates(conn: sqlite3.Connection, nanny_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pay_rates WHERE nanny_id = ? ORDER BY effective_from DESC",
        (nanny_id,),
    ).fetchall()


def current_rate(conn: sqlite3.Connection, nanny_id: int, *, on: date | None = None) -> sqlite3.Row | None:
    on = on or date.today()
    return conn.execute(
        "SELECT * FROM pay_rates "
        "WHERE nanny_id = ? AND effective_from <= ? "
        "ORDER BY effective_from DESC LIMIT 1",
        (nanny_id, on.isoformat()),
    ).fetchone()


def add_rate(
    conn: sqlite3.Connection,
    nanny_id: int,
    rate_cents: int,
    effective_from: date,
) -> int:
    cur = conn.execute(
        "INSERT INTO pay_rates (nanny_id, rate_cents, effective_from) VALUES (?, ?, ?)",
        (nanny_id, rate_cents, effective_from.isoformat()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def delete_rate(conn: sqlite3.Connection, rate_id: int) -> None:
    conn.execute("DELETE FROM pay_rates WHERE id = ?", (rate_id,))


# --- shifts ------------------------------------------------------------------

def list_shifts(
    conn: sqlite3.Connection,
    *,
    nanny_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    paid: bool | None = None,
    confirmed: bool | None = None,
    open_only: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list = []
    if nanny_id is not None:
        where.append("nanny_id = ?"); params.append(nanny_id)
    if start_date is not None:
        where.append("start_time >= ?"); params.append(start_date.isoformat())
    if end_date is not None:
        # date-exclusive upper bound
        where.append("start_time < ?"); params.append((end_date.isoformat() + "T99"))
    if paid is True:
        where.append("paid_on IS NOT NULL")
    elif paid is False:
        where.append("paid_on IS NULL")
    if confirmed is True:
        where.append("confirmed = 1")
    elif confirmed is False:
        where.append("confirmed = 0")
    if open_only:
        where.append("end_time IS NULL")
    sql = "SELECT * FROM shifts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_time DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_shift(conn: sqlite3.Connection, shift_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()


def create_shift(
    conn: sqlite3.Connection,
    *,
    nanny_id: int,
    start_time: str,
    end_time: str | None,
    rate_override_cents: int | None,
    flat_rate_cents: int | None,
    notes: str | None,
    source: str,
    confirmed: bool,
    created_by: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO shifts ("
        " nanny_id, start_time, end_time, rate_override_cents, flat_rate_cents,"
        " notes, source, confirmed, created_by, updated_by"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (nanny_id, start_time, end_time, rate_override_cents, flat_rate_cents,
         notes, source, 1 if confirmed else 0, created_by, created_by),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def update_shift(
    conn: sqlite3.Connection,
    shift_id: int,
    *,
    nanny_id: int,
    start_time: str,
    end_time: str | None,
    rate_override_cents: int | None,
    flat_rate_cents: int | None,
    notes: str | None,
    updated_by: str,
) -> None:
    conn.execute(
        "UPDATE shifts SET nanny_id=?, start_time=?, end_time=?,"
        " rate_override_cents=?, flat_rate_cents=?, notes=?,"
        " updated_by=?, updated_at=datetime('now')"
        " WHERE id = ?",
        (nanny_id, start_time, end_time, rate_override_cents, flat_rate_cents,
         notes, updated_by, shift_id),
    )


def delete_shift(conn: sqlite3.Connection, shift_id: int) -> None:
    conn.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))


def close_shift(
    conn: sqlite3.Connection, shift_id: int, end_time: str, *, updated_by: str
) -> None:
    conn.execute(
        "UPDATE shifts SET end_time=?, updated_by=?, updated_at=datetime('now')"
        " WHERE id = ? AND end_time IS NULL",
        (end_time, updated_by, shift_id),
    )


def confirm_shift(conn: sqlite3.Connection, shift_id: int, *, updated_by: str) -> None:
    conn.execute(
        "UPDATE shifts SET confirmed=1, updated_by=?, updated_at=datetime('now')"
        " WHERE id = ?",
        (updated_by, shift_id),
    )


def confirm_shifts(
    conn: sqlite3.Connection, shift_ids: list[int], *, updated_by: str
) -> int:
    """Confirm a batch of shifts. Returns the number of rows actually flipped
    (already-confirmed ones are skipped)."""
    if not shift_ids:
        return 0
    placeholders = ",".join("?" * len(shift_ids))
    cur = conn.execute(
        f"UPDATE shifts SET confirmed=1, updated_by=?, updated_at=datetime('now')"
        f" WHERE confirmed=0 AND id IN ({placeholders})",
        [updated_by, *shift_ids],
    )
    return cur.rowcount or 0


def mark_shifts_paid(
    conn: sqlite3.Connection,
    shift_ids: list[int],
    paid_on: date,
    *,
    paid_note: str | None,
    updated_by: str,
) -> None:
    if not shift_ids:
        return
    placeholders = ",".join("?" * len(shift_ids))
    params: list = [paid_on.isoformat(), paid_note, updated_by, *shift_ids]
    conn.execute(
        f"UPDATE shifts SET paid_on=?, paid_note=?, updated_by=?, updated_at=datetime('now')"
        f" WHERE id IN ({placeholders})",
        params,
    )


# --- expenses ----------------------------------------------------------------

def list_expenses(conn: sqlite3.Connection, shift_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM expenses WHERE shift_id = ? ORDER BY id", (shift_id,)
    ).fetchall()


def get_expense(conn: sqlite3.Connection, expense_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()


def create_expense(
    conn: sqlite3.Connection,
    *,
    shift_id: int,
    amount_cents: int,
    description: str,
    pending_review: bool = False,
) -> int:
    cur = conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description, pending_review)"
        " VALUES (?, ?, ?, ?)",
        (shift_id, amount_cents, description, 1 if pending_review else 0),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def update_expense(
    conn: sqlite3.Connection,
    expense_id: int,
    *,
    amount_cents: int,
    description: str,
) -> None:
    conn.execute(
        "UPDATE expenses SET amount_cents=?, description=?, pending_review=0,"
        " updated_at=datetime('now') WHERE id = ?",
        (amount_cents, description, expense_id),
    )


def delete_expense(conn: sqlite3.Connection, expense_id: int) -> None:
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))


def mark_expenses_paid(
    conn: sqlite3.Connection,
    expense_ids: list[int],
    paid_on: date,
) -> None:
    if not expense_ids:
        return
    placeholders = ",".join("?" * len(expense_ids))
    conn.execute(
        f"UPDATE expenses SET paid_on=?, updated_at=datetime('now')"
        f" WHERE id IN ({placeholders})",
        [paid_on.isoformat(), *expense_ids],
    )
