from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from shifter import pay

MEL = ZoneInfo("Australia/Melbourne")


def _mk_nanny(conn, name="Anita"):
    cur = conn.execute("INSERT INTO nannies (name) VALUES (?)", (name,))
    return cur.lastrowid


def _mk_rate(conn, nanny_id, rate_cents, effective_from):
    conn.execute(
        "INSERT INTO pay_rates (nanny_id, rate_cents, effective_from) VALUES (?, ?, ?)",
        (nanny_id, rate_cents, effective_from.isoformat()),
    )


def _mk_shift(conn, nanny_id, start, end, **kwargs):
    cols = {
        "nanny_id": nanny_id,
        "start_time": start.isoformat(),
        "end_time": end.isoformat() if end else None,
        "source": kwargs.get("source", "manual"),
        "rate_override_cents": kwargs.get("rate_override_cents"),
        "flat_rate_cents": kwargs.get("flat_rate_cents"),
        "notes": kwargs.get("notes"),
        "confirmed": kwargs.get("confirmed", 1),
        "paid_on": kwargs.get("paid_on"),
    }
    keys = ",".join(cols)
    placeholders = ",".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO shifts ({keys}) VALUES ({placeholders})", list(cols.values())
    )
    return cur.lastrowid


def test_shift_hours_basic():
    s = datetime(2026, 5, 4, 9, 0, tzinfo=MEL)
    e = datetime(2026, 5, 4, 17, 30, tzinfo=MEL)
    assert pay.shift_hours(s, e) == Decimal("8.5")


def test_shift_hours_crosses_midnight():
    s = datetime(2025, 9, 12, 23, 0, tzinfo=MEL)
    e = datetime(2025, 9, 13, 1, 0, tzinfo=MEL)
    assert pay.shift_hours(s, e) == Decimal("2")


def test_shift_hours_open_uses_now():
    s = datetime(2026, 5, 4, 9, 0, tzinfo=MEL)
    now = datetime(2026, 5, 4, 11, 30, tzinfo=MEL)
    assert pay.shift_hours(s, None, now=now) == Decimal("2.5")


def test_shift_hours_negative_clamped():
    s = datetime(2026, 5, 4, 17, 0, tzinfo=MEL)
    e = datetime(2026, 5, 4, 9, 0, tzinfo=MEL)
    assert pay.shift_hours(s, e) == Decimal(0)


def test_shift_hours_naive_rejected():
    with pytest.raises(ValueError):
        pay.shift_hours(datetime(2026, 5, 4, 9, 0), datetime(2026, 5, 4, 17, 0))


def test_shift_pay_flat_rate_wins():
    s = datetime(2026, 5, 4, 9, 0, tzinfo=MEL)
    e = datetime(2026, 5, 4, 17, 0, tzinfo=MEL)
    assert pay.shift_pay_cents(
        flat_rate_cents=15000,
        rate_override_cents=4000,
        fallback_rate_cents=3500,
        start=s, end=e,
    ) == 15000


def test_shift_pay_override_beats_fallback():
    s = datetime(2026, 5, 4, 9, 0, tzinfo=MEL)
    e = datetime(2026, 5, 4, 11, 0, tzinfo=MEL)  # 2h
    assert pay.shift_pay_cents(
        flat_rate_cents=None,
        rate_override_cents=4000,
        fallback_rate_cents=3500,
        start=s, end=e,
    ) == 8000


def test_shift_pay_uses_fallback_when_no_override():
    s = datetime(2026, 5, 4, 9, 0, tzinfo=MEL)
    e = datetime(2026, 5, 4, 11, 30, tzinfo=MEL)  # 2.5h
    assert pay.shift_pay_cents(
        flat_rate_cents=None,
        rate_override_cents=None,
        fallback_rate_cents=3500,
        start=s, end=e,
    ) == 8750


def test_shift_pay_no_rate_returns_zero():
    s = datetime(2026, 5, 4, 9, 0, tzinfo=MEL)
    e = datetime(2026, 5, 4, 11, 0, tzinfo=MEL)
    assert pay.shift_pay_cents(
        flat_rate_cents=None,
        rate_override_cents=None,
        fallback_rate_cents=None,
        start=s, end=e,
    ) == 0


def test_shift_pay_rounds_half_up():
    # 0.333... hours * 3500 cents = 1166.66... → 1167
    s = datetime(2026, 5, 4, 9, 0, tzinfo=MEL)
    e = datetime(2026, 5, 4, 9, 20, tzinfo=MEL)
    assert pay.shift_pay_cents(
        flat_rate_cents=None,
        rate_override_cents=None,
        fallback_rate_cents=3500,
        start=s, end=e,
    ) == 1167


def test_effective_rate_lookup(conn):
    nid = _mk_nanny(conn)
    _mk_rate(conn, nid, 3500, date(2025, 1, 1))
    _mk_rate(conn, nid, 3800, date(2025, 7, 1))

    assert pay.effective_rate_cents(conn, nid, date(2025, 6, 30)) == 3500
    assert pay.effective_rate_cents(conn, nid, date(2025, 7, 1)) == 3800
    assert pay.effective_rate_cents(conn, nid, date(2026, 1, 1)) == 3800
    assert pay.effective_rate_cents(conn, nid, date(2024, 12, 31)) is None


def test_compute_shift_uses_historical_rate(conn):
    nid = _mk_nanny(conn)
    _mk_rate(conn, nid, 3500, date(2025, 1, 1))
    _mk_rate(conn, nid, 3800, date(2025, 7, 1))

    sid = _mk_shift(
        conn, nid,
        datetime(2025, 6, 15, 9, 0, tzinfo=MEL),
        datetime(2025, 6, 15, 17, 0, tzinfo=MEL),
    )
    row = conn.execute("SELECT * FROM shifts WHERE id = ?", (sid,)).fetchone()
    cs = pay.compute_shift(conn, row)
    assert cs.pay_cents == 8 * 3500


def test_compute_shift_open(conn):
    nid = _mk_nanny(conn)
    _mk_rate(conn, nid, 3500, date(2025, 1, 1))
    sid = _mk_shift(conn, nid, datetime(2026, 5, 4, 9, 0, tzinfo=MEL), None)
    row = conn.execute("SELECT * FROM shifts WHERE id = ?", (sid,)).fetchone()
    now = datetime(2026, 5, 4, 11, 0, tzinfo=MEL)
    cs = pay.compute_shift(conn, row, now=now)
    assert cs.is_open is True
    assert cs.pay_cents == 7000


def test_unpaid_summary_aggregates_shifts_and_expenses(conn):
    nid = _mk_nanny(conn, "Joy")
    _mk_rate(conn, nid, 3500, date(2025, 1, 1))

    paid_sid = _mk_shift(
        conn, nid,
        datetime(2026, 4, 1, 9, 0, tzinfo=MEL),
        datetime(2026, 4, 1, 17, 0, tzinfo=MEL),
        paid_on="2026-04-15",
    )
    unpaid_sid = _mk_shift(
        conn, nid,
        datetime(2026, 5, 1, 9, 0, tzinfo=MEL),
        datetime(2026, 5, 1, 17, 0, tzinfo=MEL),
    )
    open_sid = _mk_shift(conn, nid, datetime(2026, 5, 4, 9, 0, tzinfo=MEL), None)

    conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description) VALUES (?, ?, ?)",
        (unpaid_sid, 1290, "lunch"),
    )
    conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description, paid_on) VALUES (?, ?, ?, ?)",
        (paid_sid, 5000, "aquarium", "2026-04-15"),
    )

    s = pay.unpaid_summary(conn, nid)
    assert {sh.shift_id for sh in s.shifts} == {unpaid_sid}  # paid + open both excluded
    assert s.shifts_subtotal_cents == 8 * 3500
    assert s.expenses_subtotal_cents == 1290
    assert s.grand_total_cents == 8 * 3500 + 1290


def test_unpaid_summary_includes_open_when_requested(conn):
    nid = _mk_nanny(conn)
    _mk_rate(conn, nid, 3500, date(2025, 1, 1))
    sid = _mk_shift(conn, nid, datetime(2026, 5, 4, 9, 0, tzinfo=MEL), None)
    now = datetime(2026, 5, 4, 11, 0, tzinfo=MEL)
    s = pay.unpaid_summary(conn, nid, now=now, include_open=True)
    assert sid in {sh.shift_id for sh in s.shifts}
    assert s.shifts_subtotal_cents == 7000
