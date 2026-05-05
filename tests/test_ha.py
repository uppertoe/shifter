from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shifter import ha, repos, schedule
from shifter.config import Settings

MEL = ZoneInfo("Australia/Melbourne")


def _settings(**kw):
    base = dict(api_key="testtest", dev_mode=True, ha_debounce_minutes=15)
    base.update(kw)
    return Settings(**base)


def _seed_two_nannies(conn):
    a = conn.execute("INSERT INTO nannies (name) VALUES ('Anita')").lastrowid
    j = conn.execute("INSERT INTO nannies (name) VALUES ('Joy')").lastrowid
    return a, j


# --- arrival ----------------------------------------------------------------

def test_arrival_when_one_nanny_expected(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="frigate-front", event_type_hint=None,
        settings=_settings(),
    )
    assert r.resolution == "arrival"
    assert r.nanny_id == a
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (r.shift_id,)).fetchone()
    assert shift["nanny_id"] == a
    assert shift["end_time"] is None
    assert shift["confirmed"] == 0  # HA-sourced, needs review
    assert shift["source"] == "ha"


def test_ignored_when_no_one_expected_and_no_open_shift(conn):
    """Background noise: nobody scheduled today, nothing in progress → ignore."""
    _seed_two_nannies(conn)
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source=None, event_type_hint=None,
        settings=_settings(),
    )
    assert r.resolution == "ignored"
    assert r.nanny_id is None
    # ...also true when HA tags the event as arrival
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 3, tzinfo=MEL),
        source=None, event_type_hint="arrival",
        settings=_settings(),
    )
    assert r.resolution == "ignored"


def test_no_schedule_but_open_shift_still_resolves_as_departure(conn):
    """If a shift is open (e.g. unscheduled work in progress), don't 'ignore'
    a potential departure — close it."""
    a, _ = _seed_two_nannies(conn)
    repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="manual", confirmed=True, created_by="test",
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 17, 30, tzinfo=MEL),
        source=None, event_type_hint=None,
        settings=_settings(),
    )
    assert r.resolution == "departure"


# --- staleness of long-open shifts -----------------------------------------

def test_morning_departure_does_not_close_yesterdays_open_shift(conn):
    """Parent leaves for work next morning: the still-open shift from yesterday
    must not be closed by that event. With shift_stale_hours=16, an open shift
    from 23h ago is stale → no fresh open shifts. With nobody scheduled for
    today either, the event is ignored (background noise) rather than
    burdening the unresolved queue."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="ha-webhook",
    )
    # Next morning, parent leaves for work → departure-hint event
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 5, 6, 30, tzinfo=MEL),
        source="frigate-front-door", event_type_hint="departure",
        settings=_settings(shift_stale_hours=16),
    )
    assert r.resolution == "ignored"
    yesterday = conn.execute(
        "SELECT end_time FROM shifts WHERE start_time < ?",
        ("2026-05-05T00:00:00+10:00",),
    ).fetchone()
    assert yesterday["end_time"] is None


def test_morning_departure_unresolved_when_someone_scheduled_today(conn):
    """Same scenario but a nanny is scheduled today — the morning departure
    can't be ignored because there IS activity expected. Departure hint with
    no fresh open shift → unresolved."""
    a, j = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    schedule.add_one_off(conn, nanny_id=j, on_date=date(2026, 5, 5),
                          start_time="07:00", end_time="18:00")
    repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="ha-webhook",
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 5, 6, 30, tzinfo=MEL),
        source="frigate-front-door", event_type_hint="departure",
        settings=_settings(shift_stale_hours=16),
    )
    assert r.resolution == "unresolved"
    yesterday = conn.execute(
        "SELECT end_time FROM shifts WHERE start_time < ?",
        ("2026-05-05T00:00:00+10:00",),
    ).fetchone()
    assert yesterday["end_time"] is None


def test_next_days_arrival_marks_yesterdays_shift_stale(conn):
    """Once a new shift starts, the old open shift is stale (rule a)."""
    a, j = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    schedule.add_one_off(conn, nanny_id=j, on_date=date(2026, 5, 5),
                          start_time="07:00", end_time="18:00")
    # Anita opened yesterday, never closed
    repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="ha-webhook",
    )
    # Joy arrives today → fresh shift opens cleanly
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 5, 7, 5, tzinfo=MEL),
        source="cam", event_type_hint=None,
        settings=_settings(shift_stale_hours=99),  # rule (c) inert
    )
    assert r.resolution == "arrival"
    assert r.nanny_id == j
    # Both shifts are open now: yesterday's stale + today's fresh
    open_count = conn.execute("SELECT COUNT(*) FROM shifts WHERE end_time IS NULL").fetchone()[0]
    assert open_count == 2


def test_departure_after_next_arrival_closes_only_fresh_shift(conn):
    """With both yesterday's stale + today's fresh open, a departure event
    closes today's fresh one — not yesterday's stale one."""
    a, j = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=j, on_date=date(2026, 5, 5),
                          start_time="07:00", end_time="18:00")
    # Yesterday's stale Anita shift
    stale_id = repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="ha-webhook",
    )
    # Today's fresh Joy shift
    fresh_id = repos.create_shift(
        conn, nanny_id=j,
        start_time=datetime(2026, 5, 5, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="ha-webhook",
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 5, 17, 30, tzinfo=MEL),
        source="cam-out", event_type_hint="departure",
        settings=_settings(shift_stale_hours=99),
    )
    assert r.resolution == "departure"
    assert r.shift_id == fresh_id
    stale = conn.execute("SELECT end_time FROM shifts WHERE id = ?", (stale_id,)).fetchone()
    assert stale["end_time"] is None  # untouched


def test_overnight_shift_under_threshold_still_fresh(conn):
    """7pm Mon arrival, 7am Tue departure (12h). With default 16h threshold,
    still fresh — overnight nanny case shouldn't be broken."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="19:00", end_time="08:00")
    repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 19, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="ha-webhook",
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 5, 7, 0, tzinfo=MEL),
        source="cam-out", event_type_hint="departure",
        settings=_settings(shift_stale_hours=16),
    )
    assert r.resolution == "departure"


# --- 15-min rounding -------------------------------------------------------

def test_arrival_floors_start_to_nearest_15min(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 23, tzinfo=MEL),
        source="cam", event_type_hint=None,
        settings=_settings(),
    )
    shift = conn.execute("SELECT start_time FROM shifts WHERE id = ?", (r.shift_id,)).fetchone()
    # 07:23 → floor 07:15
    assert shift["start_time"].startswith("2026-05-04T07:15:00")


def test_arrival_clamps_to_scheduled_start_when_early(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 6, 52, tzinfo=MEL),
        source="cam", event_type_hint=None,
        settings=_settings(),
    )
    shift = conn.execute("SELECT start_time FROM shifts WHERE id = ?", (r.shift_id,)).fetchone()
    # 06:52 → floor 06:45 → clamp to 07:00
    assert shift["start_time"].startswith("2026-05-04T07:00:00")


def test_departure_ceils_end_to_nearest_15min(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    arr = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="cam", event_type_hint=None,
        settings=_settings(),
    )
    # Use a different source to avoid debounce.
    ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 17, 53, tzinfo=MEL),
        source="cam-out", event_type_hint=None,
        settings=_settings(),
    )
    shift = conn.execute("SELECT end_time FROM shifts WHERE id = ?", (arr.shift_id,)).fetchone()
    # 17:53 → ceil 18:00
    assert shift["end_time"].startswith("2026-05-04T18:00:00")


def test_departure_unchanged_when_already_aligned(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    arr = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="cam", event_type_hint=None,
        settings=_settings(),
    )
    ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 18, 0, tzinfo=MEL),
        source="cam-out", event_type_hint=None,
        settings=_settings(),
    )
    shift = conn.execute("SELECT end_time FROM shifts WHERE id = ?", (arr.shift_id,)).fetchone()
    assert shift["end_time"].startswith("2026-05-04T18:00:00")


def test_manual_attribution_rounds_arrival_via_schedule(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    # Create an unresolved event by simulating an ambiguous occurrence.
    eid = conn.execute(
        "INSERT INTO ha_events (occurred_at, source, resolution)"
        " VALUES ('2026-05-04T06:48:00+10:00', 'cam', 'unresolved')",
    ).lastrowid
    r = ha.attribute_unresolved(conn, eid, nanny_id=a, direction="arrival", user="me")
    shift = conn.execute("SELECT start_time FROM shifts WHERE id = ?", (r.shift_id,)).fetchone()
    # 06:48 → floor 06:45 → clamp to 07:00
    assert shift["start_time"].startswith("2026-05-04T07:00:00")


def test_unresolved_when_two_expected_no_open(conn):
    a, j = _seed_two_nannies(conn)
    for nid in (a, j):
        schedule.add_one_off(conn, nanny_id=nid, on_date=date(2026, 5, 4),
                              start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source=None, event_type_hint=None,
        settings=_settings(),
    )
    assert r.resolution == "unresolved"


# --- departure --------------------------------------------------------------

def test_departure_closes_single_open_shift(conn):
    a, j = _seed_two_nannies(conn)
    sid = repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="test",
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 17, 30, tzinfo=MEL),
        source="frigate-front", event_type_hint=None,
        settings=_settings(),
    )
    assert r.resolution == "departure"
    assert r.shift_id == sid
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert shift["end_time"] is not None


def test_unresolved_when_two_open_shifts(conn):
    a, j = _seed_two_nannies(conn)
    for nid in (a, j):
        repos.create_shift(
            conn, nanny_id=nid,
            start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
            end_time=None, rate_override_cents=None, flat_rate_cents=None,
            notes=None, source="ha", confirmed=False, created_by="test",
        )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 17, 30, tzinfo=MEL),
        source=None, event_type_hint=None,
        settings=_settings(),
    )
    assert r.resolution == "unresolved"


# --- hints ------------------------------------------------------------------

def test_arrival_hint_creates_shift_when_one_expected(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source=None, event_type_hint="arrival",
        settings=_settings(),
    )
    assert r.resolution == "arrival"


def test_departure_hint_with_no_open_is_unresolved(conn):
    """When today *does* have schedule activity but no open shift, a 'departure'
    hint can't be attributed → unresolved (not ignored)."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 17, 30, tzinfo=MEL),
        source=None, event_type_hint="departure",
        settings=_settings(),
    )
    assert r.resolution == "unresolved"


# --- debounce ---------------------------------------------------------------

def test_debounced_event_is_ignored(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    s = _settings(ha_debounce_minutes=15)

    r1 = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="frigate-front", event_type_hint=None, settings=s,
    )
    assert r1.resolution == "arrival"

    # 5 min later from same source → debounced
    r2 = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 7, tzinfo=MEL),
        source="frigate-front", event_type_hint=None, settings=s,
    )
    assert r2.resolution == "ignored"
    # Verify no second shift created
    assert conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"] == 1


def test_debounce_window_does_not_block_after_window(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    s = _settings(ha_debounce_minutes=15)
    ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="frigate-front", event_type_hint=None, settings=s,
    )
    # 30 min later → past window, not debounced
    # State now: 1 open shift → resolves as departure
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 33, tzinfo=MEL),
        source="frigate-front", event_type_hint=None, settings=s,
    )
    assert r.resolution == "departure"


def test_debounce_isolated_per_source(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="frigate-front", event_type_hint=None, settings=_settings(),
    )
    # Different source within the window → not debounced
    # State: open shift now exists; second event becomes departure for same nanny
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 5, tzinfo=MEL),
        source="frigate-back", event_type_hint=None, settings=_settings(),
    )
    assert r.resolution == "departure"


# --- manual attribution -----------------------------------------------------

def test_attribute_unresolved_arrival(conn):
    a, j = _seed_two_nannies(conn)
    # Both expected → ambiguous → unresolved (not ignored, since schedule exists)
    for nid in (a, j):
        schedule.add_one_off(conn, nanny_id=nid, on_date=date(2026, 5, 4),
                              start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source=None, event_type_hint=None, settings=_settings(),
    )
    assert r.resolution == "unresolved"
    after = ha.attribute_unresolved(conn, r.event_id, nanny_id=a,
                                     direction="arrival", user="eamonn")
    assert after.resolution == "arrival"
    assert after.shift_id is not None
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (after.shift_id,)).fetchone()
    assert shift["created_by"] == "manual:eamonn"


def test_cannot_reattribute_already_resolved(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source=None, event_type_hint=None, settings=_settings(),
    )
    with pytest.raises(ValueError, match="already arrival"):
        ha.attribute_unresolved(conn, r.event_id, nanny_id=a,
                                  direction="arrival", user="eamonn")
