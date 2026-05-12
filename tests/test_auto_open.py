from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from shifter import repos, schedule
from shifter.config import Settings

MEL = ZoneInfo("Australia/Melbourne")


def _settings(**kw) -> Settings:
    base = dict(api_key="testtest", dev_mode=True)
    base.update(kw)
    return Settings(**base)


def _mk_nanny(conn, name: str) -> int:
    return conn.execute("INSERT INTO nannies (name) VALUES (?)", (name,)).lastrowid


# --- pending_for_date --------------------------------------------------------

def test_pending_for_date_returns_unopened_slots(conn):
    nid = _mk_nanny(conn, "Anita")
    schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    pending = schedule.pending_for_date(conn, date(2026, 5, 12), tz=MEL)
    assert len(pending) == 1
    assert pending[0].nanny_id == nid


def test_pending_for_date_excludes_cancelled(conn):
    nid = _mk_nanny(conn, "Anita")
    eid = schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    schedule.cancel_expected(conn, eid)
    assert schedule.pending_for_date(conn, date(2026, 5, 12), tz=MEL) == []


def test_pending_for_date_excludes_already_opened(conn):
    """When a shift exists overlapping the slot, the slot is no longer 'pending'."""
    nid = _mk_nanny(conn, "Anita")
    schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    # Manually open a shift inside the window
    repos.create_shift(
        conn, nanny_id=nid,
        start_time=datetime(2026, 5, 12, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None,
        rate_override_cents=None, flat_rate_cents=None, notes=None,
        source="manual", confirmed=False, created_by="test",
    )
    assert schedule.pending_for_date(conn, date(2026, 5, 12), tz=MEL) == []


# --- auto_open_due -----------------------------------------------------------

def test_auto_open_due_opens_in_progress_slot(conn):
    nid = _mk_nanny(conn, "Anita")
    schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    now = datetime(2026, 5, 12, 7, 0, tzinfo=MEL)
    created = schedule.auto_open_due(conn, now=now, settings=_settings())
    assert len(created) == 1
    shift = conn.execute(
        "SELECT * FROM shifts WHERE id = ?", (created[0],)
    ).fetchone()
    assert shift["source"] == "auto"
    assert shift["confirmed"] == 0
    assert shift["end_time"] is None
    # Shift starts at the SCHEDULED start, not at the polling-jittered `now`.
    assert datetime.fromisoformat(shift["start_time"]) == now


def test_auto_open_due_is_idempotent(conn):
    nid = _mk_nanny(conn, "Anita")
    schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    now = datetime(2026, 5, 12, 10, 0, tzinfo=MEL)
    schedule.auto_open_due(conn, now=now, settings=_settings())
    again = schedule.auto_open_due(conn, now=now, settings=_settings())
    assert again == []
    n = conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"]
    assert n == 1


def test_auto_open_due_skips_future_slot(conn):
    nid = _mk_nanny(conn, "Anita")
    schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    now = datetime(2026, 5, 12, 6, 30, tzinfo=MEL)  # 30 min before start
    assert schedule.auto_open_due(conn, now=now, settings=_settings()) == []


def test_auto_open_due_skips_finished_slot(conn):
    nid = _mk_nanny(conn, "Anita")
    schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    # 19:00 — well after end_time; don't retroactively open missed shifts.
    now = datetime(2026, 5, 12, 19, 0, tzinfo=MEL)
    assert schedule.auto_open_due(conn, now=now, settings=_settings()) == []


def test_auto_open_due_skips_cancelled_slot(conn):
    nid = _mk_nanny(conn, "Anita")
    eid = schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    schedule.cancel_expected(conn, eid)
    now = datetime(2026, 5, 12, 7, 0, tzinfo=MEL)
    assert schedule.auto_open_due(conn, now=now, settings=_settings()) == []


def test_auto_open_due_skips_when_open_shift_exists(conn):
    """If HA fired arrival first and opened a shift, the auto-opener leaves it alone."""
    nid = _mk_nanny(conn, "Anita")
    schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    repos.create_shift(
        conn, nanny_id=nid,
        start_time=datetime(2026, 5, 12, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None,
        rate_override_cents=None, flat_rate_cents=None, notes=None,
        source="ha", confirmed=False, created_by="ha-webhook",
    )
    now = datetime(2026, 5, 12, 8, 0, tzinfo=MEL)
    assert schedule.auto_open_due(conn, now=now, settings=_settings()) == []


def test_auto_open_due_handles_overnight_shift_started_yesterday(conn):
    """Overnight slot 19:00 Mon → 06:00 Tue. App restarts Tue at 02:00 — the
    yesterday-dated slot is still in progress and should be opened."""
    nid = _mk_nanny(conn, "Anita")
    schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 11),
        start_time="19:00", end_time="06:00",
    )
    now = datetime(2026, 5, 12, 2, 0, tzinfo=MEL)
    created = schedule.auto_open_due(conn, now=now, settings=_settings())
    assert len(created) == 1
    shift = conn.execute(
        "SELECT * FROM shifts WHERE id = ?", (created[0],)
    ).fetchone()
    # Starts at 19:00 on the slot's date, not at restart time.
    assert datetime.fromisoformat(shift["start_time"]) == \
        datetime(2026, 5, 11, 19, 0, tzinfo=MEL)


# --- update_expected_times --------------------------------------------------

def test_update_expected_times(conn):
    nid = _mk_nanny(conn, "Anita")
    eid = schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 12),
        start_time="07:00", end_time="18:00",
    )
    schedule.update_expected_times(conn, eid, start_time="07:30", end_time="18:30")
    row = conn.execute(
        "SELECT start_time, end_time FROM expected_shifts WHERE id = ?", (eid,)
    ).fetchone()
    assert row["start_time"] == "07:30"
    assert row["end_time"] == "18:30"


def test_slot_window_overnight(conn):
    nid = _mk_nanny(conn, "Anita")
    eid = schedule.add_one_off(
        conn, nanny_id=nid, on_date=date(2026, 5, 11),
        start_time="19:00", end_time="06:00",
    )
    slot = schedule.expected_on_date(conn, date(2026, 5, 11))[0]
    start, end = schedule.slot_window(slot, MEL)
    assert start == datetime(2026, 5, 11, 19, 0, tzinfo=MEL)
    assert end == datetime(2026, 5, 12, 6, 0, tzinfo=MEL)
