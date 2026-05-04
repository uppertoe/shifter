from __future__ import annotations

from datetime import date

from shifter import schedule


def _mk_nanny(conn, name):
    cur = conn.execute("INSERT INTO nannies (name) VALUES (?)", (name,))
    return cur.lastrowid


def test_materialize_creates_weekly_rows(conn):
    nid = _mk_nanny(conn, "Anita")
    pid = schedule.create_pattern(
        conn, nanny_id=nid, day_of_week=2,  # Wednesday
        start_time="07:00", end_time="18:00",
        active_from=date(2026, 5, 1),
    )
    n = schedule.materialize(conn, from_date=date(2026, 5, 1), to_date=date(2026, 5, 31))
    # 2026-05-06, 13, 20, 27 are Wednesdays — 4 weeks
    assert n == 4
    rows = schedule.expected_in_range(conn, start=date(2026, 5, 1), end=date(2026, 6, 1))
    assert all(r.date.weekday() == 2 for r in rows)
    assert {r.date.isoformat() for r in rows} == {
        "2026-05-06", "2026-05-13", "2026-05-20", "2026-05-27"
    }
    assert all(r.pattern_id == pid for r in rows)
    assert all(r.source == "pattern" for r in rows)


def test_materialize_idempotent(conn):
    nid = _mk_nanny(conn, "Anita")
    schedule.create_pattern(conn, nanny_id=nid, day_of_week=2,
                             start_time="07:00", end_time="18:00",
                             active_from=date(2026, 5, 1))
    schedule.materialize(conn, from_date=date(2026, 5, 1), to_date=date(2026, 5, 31))
    n2 = schedule.materialize(conn, from_date=date(2026, 5, 1), to_date=date(2026, 5, 31))
    assert n2 == 0  # unique constraint blocks dupes


def test_materialize_respects_active_until(conn):
    nid = _mk_nanny(conn, "Joy")
    schedule.create_pattern(conn, nanny_id=nid, day_of_week=2,
                             start_time="07:00", end_time="18:00",
                             active_from=date(2026, 5, 1),
                             active_until=date(2026, 5, 14))  # only first 2 Wednesdays
    schedule.materialize(conn, from_date=date(2026, 5, 1), to_date=date(2026, 5, 31))
    rows = schedule.expected_in_range(conn, start=date(2026, 5, 1), end=date(2026, 6, 1))
    assert {r.date.isoformat() for r in rows} == {"2026-05-06", "2026-05-13"}


def test_add_one_off_takes_precedence_over_pattern(conn):
    nid = _mk_nanny(conn, "Anita")
    schedule.create_pattern(conn, nanny_id=nid, day_of_week=2,
                             start_time="07:00", end_time="18:00",
                             active_from=date(2026, 5, 1))
    schedule.materialize(conn, from_date=date(2026, 5, 1), to_date=date(2026, 5, 31))
    # User cancels Wed 13th
    rows = schedule.expected_in_range(conn, start=date(2026, 5, 13), end=date(2026, 5, 14))
    schedule.cancel_expected(conn, rows[0].expected_id)
    rows = schedule.expected_in_range(conn, start=date(2026, 5, 13), end=date(2026, 5, 14))
    assert rows[0].cancelled is True
    # Re-materialize: should NOT un-cancel
    schedule.materialize(conn, from_date=date(2026, 5, 1), to_date=date(2026, 5, 31))
    rows = schedule.expected_in_range(conn, start=date(2026, 5, 13), end=date(2026, 5, 14))
    assert rows[0].cancelled is True


def test_add_one_off_revives_cancelled(conn):
    nid = _mk_nanny(conn, "Joy")
    eid = schedule.add_one_off(conn, nanny_id=nid, on_date=date(2026, 5, 5),
                                start_time="08:00", end_time="14:00")
    schedule.cancel_expected(conn, eid)
    # Adding the same slot un-cancels rather than creating a duplicate.
    eid2 = schedule.add_one_off(conn, nanny_id=nid, on_date=date(2026, 5, 5),
                                 start_time="08:00", end_time="14:00")
    assert eid2 == eid
    row = conn.execute("SELECT cancelled, source FROM expected_shifts WHERE id = ?", (eid,)).fetchone()
    assert row["cancelled"] == 0
    assert row["source"] == "manual"


def test_add_one_off_does_not_reclassify_pattern_row(conn):
    """Clicking + on an existing pattern slot should leave it as 'pattern'."""
    nid = _mk_nanny(conn, "Anita")
    schedule.create_pattern(
        conn, nanny_id=nid, day_of_week=2,
        start_time="07:00", end_time="18:00",
        active_from=date(2026, 5, 1),
    )
    schedule.materialize(conn, from_date=date(2026, 5, 1), to_date=date(2026, 5, 31))
    # Cancel one of the pattern occurrences
    rows = schedule.expected_in_range(conn, start=date(2026, 5, 13), end=date(2026, 5, 14),
                                       include_cancelled=True)
    schedule.cancel_expected(conn, rows[0].expected_id)
    # User clicks + to restore — should un-cancel, NOT reclassify to manual
    schedule.add_one_off(conn, nanny_id=nid, on_date=date(2026, 5, 13),
                          start_time="07:00", end_time="18:00")
    row = conn.execute(
        "SELECT cancelled, source FROM expected_shifts WHERE id = ?",
        (rows[0].expected_id,),
    ).fetchone()
    assert row["cancelled"] == 0
    assert row["source"] == "pattern"


def test_delete_pattern_removes_future_pattern_rows(conn):
    nid = _mk_nanny(conn, "Anita")
    pid = schedule.create_pattern(conn, nanny_id=nid, day_of_week=2,
                                   start_time="07:00", end_time="18:00",
                                   active_from=date(2020, 1, 1))  # historical
    # Force-create a row in the past via direct insert (so we can confirm it survives)
    conn.execute(
        "INSERT INTO expected_shifts (nanny_id, date, start_time, end_time, pattern_id, source) "
        "VALUES (?, ?, ?, ?, ?, 'pattern')",
        (nid, "2020-01-01", "07:00", "18:00", pid),
    )
    schedule.materialize(conn, from_date=date.today(), to_date=date.today())
    schedule.delete_pattern(conn, pid)
    # Past row still there
    past = conn.execute("SELECT COUNT(*) c FROM expected_shifts WHERE date = '2020-01-01'").fetchone()["c"]
    assert past == 1


def test_expected_on_date_excludes_cancelled_by_default(conn):
    nid = _mk_nanny(conn, "Anita")
    eid = schedule.add_one_off(conn, nanny_id=nid, on_date=date(2026, 5, 5),
                                start_time="08:00", end_time="14:00")
    assert len(schedule.expected_on_date(conn, date(2026, 5, 5))) == 1
    schedule.cancel_expected(conn, eid)
    assert len(schedule.expected_on_date(conn, date(2026, 5, 5))) == 0
    assert len(schedule.expected_on_date(conn, date(2026, 5, 5), include_cancelled=True)) == 1
