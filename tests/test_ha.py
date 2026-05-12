from __future__ import annotations

from datetime import date, datetime
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


# --- arrival: state A → state B --------------------------------------------

def test_arrival_when_one_nanny_expected(conn):
    """Booked shift (state A) consumes an arrival event → state-B shift."""
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
    assert shift["confirmed"] == 0
    assert shift["source"] == "ha"


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


def test_unscheduled_arrival_is_ignored(conn):
    """No expected_shifts row for the day → no state-A waiting → arrival
    event is noise. The schedule does the filtering."""
    _seed_two_nannies(conn)
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="frigate-front", event_type_hint="arrival",
        settings=_settings(),
    )
    assert r.resolution == "ignored"
    assert conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"] == 0


def test_arrival_event_when_nanny_already_arrived_attaches(conn):
    """The nanny has a fresh open shift (state B) → no new shift to create,
    but the event attaches to that shift so its snapshot lights up. This is
    the auto-opener-then-HA case and the duplicate-HA case (two cameras)."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    sid = repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="ha-webhook",
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 9, 0, tzinfo=MEL),
        source="frigate-front", event_type_hint="arrival",
        settings=_settings(),
    )
    assert r.resolution == "arrival"
    assert r.shift_id == sid
    # No duplicate shift created.
    assert conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"] == 1


def test_two_expected_no_open_shift_arrival_is_ignored(conn):
    """Two state-A waiting → ambiguous nanny → ignored. (Was previously
    'unresolved'; the unresolved-events queue is gone.)"""
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
    assert r.resolution == "ignored"


# --- departure: state B → state D ------------------------------------------

def test_departure_closes_single_open_shift(conn):
    """Fresh open shift (B) consumes a no-hint event when no arrival
    candidate is waiting → state D, confirmed=0."""
    a, _ = _seed_two_nannies(conn)
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
    assert shift["confirmed"] == 0


def test_no_schedule_but_open_shift_still_resolves_as_departure(conn):
    """A manually-created fresh shift (no schedule entry) still consumes a
    departure event — the shift itself is the activity that matters."""
    a, _ = _seed_two_nannies(conn)
    sid = repos.create_shift(
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
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert shift["confirmed"] == 0  # ALL HA closes need re-confirmation


def test_departure_hint_with_no_open_is_ignored(conn):
    """Departure hint, no shift to consume it → ignored."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 17, 30, tzinfo=MEL),
        source=None, event_type_hint="departure",
        settings=_settings(),
    )
    assert r.resolution == "ignored"


def test_two_open_shifts_no_hint_is_ignored(conn):
    """Multiple fresh open shifts → ambiguous which to close → ignored."""
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
    assert r.resolution == "ignored"


# --- background noise -------------------------------------------------------

def test_ignored_when_no_one_expected_and_no_open_shift(conn):
    """Nobody scheduled, nothing in progress → background noise."""
    _seed_two_nannies(conn)
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source=None, event_type_hint=None,
        settings=_settings(),
    )
    assert r.resolution == "ignored"
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 3, tzinfo=MEL),
        source=None, event_type_hint="arrival",
        settings=_settings(),
    )
    assert r.resolution == "ignored"


# --- staleness: state C is invisible to the resolver -----------------------

def test_morning_departure_does_not_close_yesterdays_stale_shift(conn):
    """Stale shifts (state C) no longer accept events. Yesterday's open
    shift, with nobody scheduled today, is invisible to the resolver — the
    morning departure event is background noise."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    sid = repos.create_shift(
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
    assert r.resolution == "ignored"
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert shift["end_time"] is None  # untouched


def test_morning_departure_ignored_even_when_someone_scheduled_today(conn):
    """Schedule activity today doesn't 'unlock' yesterday's stale shift —
    stale stays stale. Departure-hint event finds no fresh open shift to
    consume → ignored."""
    a, j = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    schedule.add_one_off(conn, nanny_id=j, on_date=date(2026, 5, 5),
                          start_time="07:00", end_time="18:00")
    sid = repos.create_shift(
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
    assert r.resolution == "ignored"
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert shift["end_time"] is None


def test_next_days_arrival_marks_yesterdays_shift_stale_via_rule_a(conn):
    """Once a new shift starts, the previous one is stale (rule a). The
    new arrival creates a fresh shift cleanly; the old stale one is now
    surfaced for human resolution."""
    a, j = _seed_two_nannies(conn)
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
        occurred_at=datetime(2026, 5, 5, 7, 5, tzinfo=MEL),
        source="cam", event_type_hint=None,
        settings=_settings(shift_stale_hours=99),
    )
    assert r.resolution == "arrival"
    assert r.nanny_id == j
    open_count = conn.execute("SELECT COUNT(*) FROM shifts WHERE end_time IS NULL").fetchone()[0]
    assert open_count == 2


def test_departure_with_stale_and_fresh_open_closes_only_fresh(conn):
    """Stale + fresh open shifts: departure event consumes the fresh one
    (state C is invisible to the resolver)."""
    a, j = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=j, on_date=date(2026, 5, 5),
                          start_time="07:00", end_time="18:00")
    stale_id = repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="ha-webhook",
    )
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


def test_overnight_shift_under_threshold_still_consumes_departure(conn):
    """7pm Mon → 7am Tue is 12h, still fresh under the default 16h threshold;
    departure event closes it normally."""
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
    ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 17, 53, tzinfo=MEL),
        source="cam-out", event_type_hint=None,
        settings=_settings(),
    )
    shift = conn.execute("SELECT end_time FROM shifts WHERE id = ?", (arr.shift_id,)).fetchone()
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


# --- HA-driven close always unconfirms (ALL shifts need human review) -------

def test_ha_close_unconfirms_previously_confirmed_shift(conn):
    """A manually-created confirmed shift, then closed by an HA departure
    event, flips back to confirmed=0 — the auto-set end time needs the
    human's eyes against the screenshot."""
    a, _ = _seed_two_nannies(conn)
    sid = repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="manual", confirmed=True, created_by="eamonn",
    )
    ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 17, 30, tzinfo=MEL),
        source="cam", event_type_hint="departure",
        settings=_settings(),
    )
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert shift["confirmed"] == 0
    assert shift["end_time"] is not None


def test_update_shift_can_clear_end_time_to_reopen(conn):
    """Editing a closed (state D/E) shift to blank its end_time re-opens
    it (back to state B). repos.update_shift writes through whatever is
    passed."""
    a, _ = _seed_two_nannies(conn)
    sid = repos.create_shift(
        conn, nanny_id=a,
        start_time="2026-05-04T07:00:00+10:00",
        end_time="2026-05-04T15:00:00+10:00",
        rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="ha", confirmed=False, created_by="ha-webhook",
    )
    repos.update_shift(
        conn, sid,
        nanny_id=a,
        start_time="2026-05-04T07:00:00+10:00",
        end_time=None,
        rate_override_cents=None, flat_rate_cents=None,
        notes=None, updated_by="eamonn",
    )
    shift = conn.execute("SELECT end_time FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert shift["end_time"] is None


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
    r2 = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 7, tzinfo=MEL),
        source="frigate-front", event_type_hint=None, settings=s,
    )
    assert r2.resolution == "ignored"
    assert conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"] == 1


def test_debounce_window_does_not_block_after_window(conn):
    """Past the debounce window, the event is processed normally. With a
    fresh open shift, the no-hint event closes it."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    s = _settings(ha_debounce_minutes=15)
    ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="frigate-front", event_type_hint=None, settings=s,
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 33, tzinfo=MEL),
        source="frigate-front", event_type_hint=None, settings=s,
    )
    assert r.resolution == "departure"


def test_debounce_isolated_per_source(conn):
    """Different sources within the debounce window are processed
    independently — the second event closes the now-open shift."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="frigate-front", event_type_hint=None, settings=_settings(),
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 5, tzinfo=MEL),
        source="frigate-back", event_type_hint=None, settings=_settings(),
    )
    assert r.resolution == "departure"


# --- legacy: attribute_unresolved still works for any unresolved rows in DB

def test_attribute_unresolved_arrival_works_for_legacy_rows(conn):
    """The resolver no longer creates 'unresolved' events, but the
    attribution helper is kept so existing unresolved rows in the DB can
    still be cleared by hand at /api/events/unresolved."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    eid = conn.execute(
        "INSERT INTO ha_events (occurred_at, source, resolution)"
        " VALUES ('2026-05-04T06:48:00+10:00', 'cam', 'unresolved')",
    ).lastrowid
    r = ha.attribute_unresolved(conn, eid, nanny_id=a, direction="arrival", user="me")
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (r.shift_id,)).fetchone()
    assert shift["start_time"].startswith("2026-05-04T07:00:00")
    assert shift["created_by"].startswith("manual:")


def test_cannot_reattribute_already_resolved(conn):
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source=None, event_type_hint=None, settings=_settings(),
    )
    assert r.resolution == "arrival"
    with pytest.raises(ValueError, match="already arrival"):
        ha.attribute_unresolved(conn, r.event_id, nanny_id=a,
                                  direction="arrival", user="eamonn")


# --- arrival attach: HA fires after shift was already opened ----------------

def test_arrival_hint_attaches_when_shift_already_open(conn):
    """Auto-opener fired first → an HA arrival event for the same slot now
    has no new shift to create, but should still attach to the open shift so
    its snapshot lights up on the dashboard."""
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    # Simulate auto-opener creating the shift at the scheduled start
    sid = repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None,
        rate_override_cents=None, flat_rate_cents=None, notes=None,
        source="auto", confirmed=False, created_by="auto-opener",
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="frigate-front", event_type_hint="arrival",
        settings=_settings(),
    )
    assert r.resolution == "arrival"
    assert r.shift_id == sid                # attached to the auto-opened shift
    assert r.nanny_id == a
    # Only one shift exists — no duplicate created
    assert conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"] == 1
    # The auto-opened shift's start_time is untouched
    row = conn.execute("SELECT start_time FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert row["start_time"] == datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat()
    # The event row links to the shift so screenshots will surface via the join
    evt = conn.execute(
        "SELECT shift_id, resolution, resolution_note FROM ha_events WHERE id = ?",
        (r.event_id,),
    ).fetchone()
    assert evt["shift_id"] == sid
    assert evt["resolution"] == "arrival"
    assert "attached" in (evt["resolution_note"] or "")


def test_arrival_attach_ignored_when_two_nannies_expected(conn):
    """Ambiguous: two nannies expected, both have open shifts. Don't guess."""
    a, j = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    schedule.add_one_off(conn, nanny_id=j, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    for nid in (a, j):
        repos.create_shift(
            conn, nanny_id=nid,
            start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
            end_time=None,
            rate_override_cents=None, flat_rate_cents=None, notes=None,
            source="auto", confirmed=False, created_by="auto-opener",
        )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 5, tzinfo=MEL),
        source="frigate-front", event_type_hint="arrival",
        settings=_settings(),
    )
    assert r.resolution == "ignored"
    assert r.shift_id is None


def test_arrival_attach_screenshots_join_through(conn):
    """End-to-end: attach an arrival event to an auto-opened shift, upload a
    screenshot against the event, confirm shots_for_shift returns it."""
    from shifter import screenshots
    a, _ = _seed_two_nannies(conn)
    schedule.add_one_off(conn, nanny_id=a, on_date=date(2026, 5, 4),
                          start_time="07:00", end_time="18:00")
    sid = repos.create_shift(
        conn, nanny_id=a,
        start_time=datetime(2026, 5, 4, 7, 0, tzinfo=MEL).isoformat(),
        end_time=None,
        rate_override_cents=None, flat_rate_cents=None, notes=None,
        source="auto", confirmed=False, created_by="auto-opener",
    )
    r = ha.process_event(
        conn,
        occurred_at=datetime(2026, 5, 4, 7, 2, tzinfo=MEL),
        source="frigate-front", event_type_hint="arrival",
        settings=_settings(),
    )
    # Insert a screenshot row directly — same shape as the webhook would.
    conn.execute(
        "INSERT INTO screenshots (ha_event_id, filename, content_type, size_bytes)"
        " VALUES (?, ?, ?, ?)",
        (r.event_id, "fake.jpg", "image/jpeg", 1234),
    )
    shots = screenshots.shots_for_shift(conn, sid)
    assert shots["arrival"] is not None
    assert shots["arrival"]["filename"] == "fake.jpg"
