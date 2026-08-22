"""Tests for the signal-based HA integration (ha_signals.process_signal).

Scenarios covered:
  - Normal nanny arrival via keypad (access_granted)
  - Arrival ignored when no homeowner present
  - Arrival ignored when no expected shift in window
  - Arrival ignored when shift already open
  - Let-in fallback arrival (access_denied → entry_pir within window)
  - Let-in ignored when access_denied too stale
  - Let-in ignored when no homeowner present
  - Departure detected (full departure_watch active)
  - Departure suppressed by entry_suppression window
  - Departure not triggered when no homeowner_home after shift start
  - Departure not triggered when no homeowner present
  - Departure not triggered when multiple open shifts (ambiguous)
  - Presence signals always recorded, never resolved
  - access_denied always recorded
  - Multiple homeowners: count >= 1 is sufficient
  - Homeowner_count reflects latest signal per person
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shifter import ha_signals, schedule
from shifter.config import Settings

MEL = ZoneInfo("Australia/Melbourne")
DAY = date(2026, 5, 14)


def _settings(**kw):
    base = dict(
        api_key="testtest",
        dev_mode=True,
        pre_shift_window_minutes=90,
        entry_suppression_minutes=3,
        failed_entry_window_minutes=5,
        shift_stale_hours=16,
    )
    base.update(kw)
    return Settings(**base)


def _seed(conn):
    nanny = conn.execute("INSERT INTO nannies (name) VALUES ('Grace')").lastrowid
    homeowner = "alex_homeowner"
    return nanny, homeowner


def _add_expected(conn, nanny_id, start="07:00", end="18:00"):
    schedule.add_one_off(conn, nanny_id=nanny_id, on_date=DAY,
                         start_time=start, end_time=end)


def _signal(conn, *, signal, source="rosslare", person=None,
            dt=None, settings=None):
    if dt is None:
        dt = datetime(2026, 5, 14, 7, 2, tzinfo=MEL)
    if settings is None:
        settings = _settings()
    return ha_signals.process_signal(
        conn,
        occurred_at=dt,
        source=source,
        signal=signal,
        person=person,
        settings=settings,
    )


def _presence_home(conn, person="alex_homeowner", dt=None):
    if dt is None:
        dt = datetime(2026, 5, 14, 6, 0, tzinfo=MEL)
    _signal(conn, signal="homeowner_home", source="ha_presence",
            person=person, dt=dt)


def _presence_away(conn, person="alex_homeowner", dt=None):
    if dt is None:
        dt = datetime(2026, 5, 14, 6, 0, tzinfo=MEL)
    _signal(conn, signal="homeowner_away", source="ha_presence",
            person=person, dt=dt)


# ---------------------------------------------------------------------------
# Arrival via access_granted
# ---------------------------------------------------------------------------

def test_arrival_access_granted_normal(conn):
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny)
    _presence_home(conn, hw)

    r = _signal(conn, signal="access_granted",
                dt=datetime(2026, 5, 14, 7, 2, tzinfo=MEL))

    assert r.resolution == "arrival"
    assert r.nanny_id == nanny
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (r.shift_id,)).fetchone()
    assert shift["nanny_id"] == nanny
    assert shift["end_time"] is None
    assert shift["source"] == "ha"
    assert shift["confirmed"] == 0


def test_arrival_no_homeowner(conn):
    nanny, _ = _seed(conn)
    _add_expected(conn, nanny)
    # No homeowner_home signal sent

    r = _signal(conn, signal="access_granted",
                dt=datetime(2026, 5, 14, 7, 2, tzinfo=MEL))

    assert r.resolution == "ignored"
    assert conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 0


def test_arrival_no_expected_shift(conn):
    _, hw = _seed(conn)
    _presence_home(conn, hw)
    # No schedule entry

    r = _signal(conn, signal="access_granted",
                dt=datetime(2026, 5, 14, 7, 2, tzinfo=MEL))

    assert r.resolution == "ignored"


def test_arrival_outside_window(conn):
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny, start="07:00")
    _presence_home(conn, hw)

    # 10:00 — 3h after scheduled start, well outside 90-min window
    r = _signal(conn, signal="access_granted",
                dt=datetime(2026, 5, 14, 10, 0, tzinfo=MEL))

    assert r.resolution == "ignored"


def test_arrival_early_within_window(conn):
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny, start="08:00")
    _presence_home(conn, hw)

    # 07:05 — 55 min before scheduled start, within 90-min window
    r = _signal(conn, signal="access_granted",
                dt=datetime(2026, 5, 14, 7, 5, tzinfo=MEL))

    assert r.resolution == "arrival"


def test_arrival_recorded_when_shift_already_open(conn):
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny)
    _presence_home(conn, hw)

    # First arrival opens the shift
    r1 = _signal(conn, signal="access_granted",
                 dt=datetime(2026, 5, 14, 7, 2, tzinfo=MEL))
    assert r1.resolution == "arrival"

    # Second access_granted (homeowner returning) → recorded, not another arrival
    r2 = _signal(conn, signal="access_granted",
                 dt=datetime(2026, 5, 14, 16, 0, tzinfo=MEL))
    assert r2.resolution == "recorded"
    assert conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Failed-entry fallback (access_denied → entry_pir)
# ---------------------------------------------------------------------------

def test_let_in_arrival(conn):
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny)
    _presence_home(conn, hw)

    denied_at = datetime(2026, 5, 14, 7, 2, tzinfo=MEL)
    pir_at = denied_at + timedelta(minutes=2)

    _signal(conn, signal="access_denied", dt=denied_at)
    r = _signal(conn, signal="entry_pir", source="entry_pir", dt=pir_at)

    assert r.resolution == "arrival"
    assert r.nanny_id == nanny


def test_let_in_too_late(conn):
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny)
    _presence_home(conn, hw)

    denied_at = datetime(2026, 5, 14, 7, 2, tzinfo=MEL)
    pir_at = denied_at + timedelta(minutes=6)  # beyond 5-min window

    _signal(conn, signal="access_denied", dt=denied_at)
    r = _signal(conn, signal="entry_pir", source="entry_pir", dt=pir_at)

    assert r.resolution == "recorded"


def test_let_in_no_homeowner(conn):
    nanny, _ = _seed(conn)
    _add_expected(conn, nanny)
    # No homeowner_home

    denied_at = datetime(2026, 5, 14, 7, 2, tzinfo=MEL)
    pir_at = denied_at + timedelta(minutes=2)

    _signal(conn, signal="access_denied", dt=denied_at)
    r = _signal(conn, signal="entry_pir", source="entry_pir", dt=pir_at)

    assert r.resolution == "recorded"
    assert conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 0


def test_let_in_not_triggered_by_frigate(conn):
    """Frigate signals don't trigger the failed-entry fallback."""
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny)
    _presence_home(conn, hw)

    denied_at = datetime(2026, 5, 14, 7, 2, tzinfo=MEL)
    _signal(conn, signal="access_denied", dt=denied_at)

    r = _signal(conn, signal="verandah_person", source="frigate_verandah",
                dt=denied_at + timedelta(minutes=1))
    assert r.resolution == "recorded"
    assert conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Departure detection
# ---------------------------------------------------------------------------

def _open_shift(conn, nanny_id, start="2026-05-14T07:00:00+10:00"):
    from shifter import repos
    return repos.create_shift(
        conn, nanny_id=nanny_id, start_time=start,
        end_time=None, rate_override_cents=None,
        flat_rate_cents=None, notes=None,
        source="ha", confirmed=False,
        created_by="test",
    )


def test_departure_via_entry_pir(conn):
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny)
    # Homeowner arrives AFTER shift start
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 17, 0, tzinfo=MEL))

    r = _signal(conn, signal="entry_pir", source="entry_pir",
                dt=datetime(2026, 5, 14, 17, 30, tzinfo=MEL))

    assert r.resolution == "departure"
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (r.shift_id,)).fetchone()
    assert shift["end_time"] is not None
    assert shift["confirmed"] == 0


def test_departure_via_front_deck_person(conn):
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny)
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 17, 0, tzinfo=MEL))
    # Homeowner confirmed by keypad before Frigate fires
    _signal(conn, signal="access_granted", dt=datetime(2026, 5, 14, 17, 15, tzinfo=MEL))

    r = _signal(conn, signal="front_deck_person", source="frigate_front_deck",
                dt=datetime(2026, 5, 14, 17, 30, tzinfo=MEL))

    assert r.resolution == "departure"


def test_departure_via_verandah_person(conn):
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny)
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 17, 0, tzinfo=MEL))
    # Homeowner confirmed by keypad before Frigate fires
    _signal(conn, signal="access_granted", dt=datetime(2026, 5, 14, 17, 15, tzinfo=MEL))

    r = _signal(conn, signal="verandah_person", source="frigate_verandah",
                dt=datetime(2026, 5, 14, 17, 30, tzinfo=MEL))

    assert r.resolution == "departure"


def test_frigate_departure_suppressed_without_keypad_confirmation(conn):
    """GPS fires homeowner_home but homeowner hasn't reached keypad yet — Frigate sees
    homeowner approaching, not nanny departing.  Departure must be suppressed.
    """
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny)
    # GPS ping — homeowner is still in car/driveway
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 12, 9, tzinfo=MEL))
    # No access_granted yet

    # Frigate sees person on verandah 3 min later (homeowner approaching)
    r = _signal(conn, signal="verandah_person", source="frigate_verandah",
                dt=datetime(2026, 5, 14, 12, 12, tzinfo=MEL))

    assert r.resolution == "recorded"
    assert "GPS not yet confirmed" in r.note
    assert conn.execute("SELECT end_time FROM shifts").fetchone()["end_time"] is None


def test_late_frigate_snapshot_attaches_to_pir_closed_shift(conn):
    """PIR closes shift; Frigate fires within 30 s as nanny steps onto verandah.
    The Frigate signal should resolve as 'departure' on the just-closed shift so
    shots_for_shift() surfaces the verandah photo.
    """
    nanny, hw = _seed(conn)
    shift_id = _open_shift(conn, nanny)
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 17, 0, tzinfo=MEL))
    _signal(conn, signal="access_granted", dt=datetime(2026, 5, 14, 17, 15, tzinfo=MEL))

    pir_at = datetime(2026, 5, 14, 18, 0, tzinfo=MEL)
    r_pir = _signal(conn, signal="entry_pir", source="entry_pir", dt=pir_at)
    assert r_pir.resolution == "departure"

    frigate_at = pir_at + timedelta(seconds=20)
    r_frigate = _signal(conn, signal="verandah_person", source="frigate_verandah",
                        dt=frigate_at)

    assert r_frigate.resolution == "departure"
    assert r_frigate.shift_id == r_pir.shift_id
    assert r_frigate.nanny_id == nanny


def test_late_frigate_not_attached_after_30s(conn):
    """Frigate more than 30 s after PIR departure is not attached."""
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny)
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 17, 0, tzinfo=MEL))
    _signal(conn, signal="access_granted", dt=datetime(2026, 5, 14, 17, 15, tzinfo=MEL))

    pir_at = datetime(2026, 5, 14, 18, 0, tzinfo=MEL)
    _signal(conn, signal="entry_pir", source="entry_pir", dt=pir_at)

    frigate_at = pir_at + timedelta(seconds=35)
    r = _signal(conn, signal="verandah_person", source="frigate_verandah", dt=frigate_at)
    assert r.resolution == "recorded"


def test_access_granted_attaches_to_auto_created_shift(conn):
    """When the shift is pre-created (auto) and access_granted fires within the
    expected window, the signal resolves as 'arrival' on the existing shift so
    shots_for_shift() can surface the keypad-time snapshot.  No new shift is created.
    """
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny)
    shift_id = _open_shift(conn, nanny, start="2026-05-14T07:00:00+10:00")

    r = _signal(conn, signal="access_granted",
                dt=datetime(2026, 5, 14, 7, 5, tzinfo=MEL))

    assert r.resolution == "arrival"
    assert r.shift_id == shift_id
    assert r.nanny_id == nanny
    assert conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 1


def test_access_granted_not_attached_outside_window(conn):
    """Homeowner returning hours after shift start should NOT attach as arrival."""
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny, start="07:00")
    _open_shift(conn, nanny, start="2026-05-14T07:00:00+10:00")

    r = _signal(conn, signal="access_granted",
                dt=datetime(2026, 5, 14, 16, 0, tzinfo=MEL))

    assert r.resolution == "recorded"
    assert conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 1


def test_departure_suppressed_by_entry_suppression(conn):
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny)
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 17, 0, tzinfo=MEL))

    # Homeowner enters via keypad (access_granted within suppression window)
    keypad_at = datetime(2026, 5, 14, 17, 30, tzinfo=MEL)
    _signal(conn, signal="access_granted", dt=keypad_at)

    # PIR fires 1 min later — inside the 3-min suppression window
    pir_at = keypad_at + timedelta(minutes=1)
    r = _signal(conn, signal="entry_pir", source="entry_pir", dt=pir_at)

    assert r.resolution == "recorded"
    assert conn.execute("SELECT end_time FROM shifts").fetchone()["end_time"] is None


def test_departure_not_suppressed_after_window(conn):
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny)
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 17, 0, tzinfo=MEL))

    keypad_at = datetime(2026, 5, 14, 17, 30, tzinfo=MEL)
    _signal(conn, signal="access_granted", dt=keypad_at)

    # PIR fires 4 min later — outside the 3-min suppression window
    pir_at = keypad_at + timedelta(minutes=4)
    r = _signal(conn, signal="entry_pir", source="entry_pir", dt=pir_at)

    assert r.resolution == "departure"


def test_departure_watch_inactive_without_homeowner_home_after_shift(conn):
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny, start="2026-05-14T07:00:00+10:00")
    # Homeowner signal BEFORE shift start — watch should NOT activate
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 6, 0, tzinfo=MEL))

    r = _signal(conn, signal="entry_pir", source="entry_pir",
                dt=datetime(2026, 5, 14, 17, 30, tzinfo=MEL))

    assert r.resolution == "recorded"


def test_departure_watch_inactive_when_no_homeowner_present(conn):
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny)
    # Homeowner left
    _presence_away(conn, hw, dt=datetime(2026, 5, 14, 14, 0, tzinfo=MEL))

    r = _signal(conn, signal="entry_pir", source="entry_pir",
                dt=datetime(2026, 5, 14, 17, 30, tzinfo=MEL))

    assert r.resolution == "recorded"


def test_departure_watch_inactive_multiple_open_shifts(conn):
    """Ambiguous — two open shifts → watch inactive."""
    nanny2 = conn.execute("INSERT INTO nannies (name) VALUES ('Bea')").lastrowid
    nanny, hw = _seed(conn)
    _open_shift(conn, nanny)
    _open_shift(conn, nanny2)
    _presence_home(conn, hw, dt=datetime(2026, 5, 14, 17, 0, tzinfo=MEL))

    r = _signal(conn, signal="entry_pir", source="entry_pir",
                dt=datetime(2026, 5, 14, 17, 30, tzinfo=MEL))

    assert r.resolution == "recorded"


# ---------------------------------------------------------------------------
# Presence signals
# ---------------------------------------------------------------------------

def test_homeowner_home_always_recorded(conn):
    r = _signal(conn, signal="homeowner_home", source="ha_presence",
                person="alex_homeowner")
    assert r.resolution == "recorded"


def test_homeowner_away_always_recorded(conn):
    r = _signal(conn, signal="homeowner_away", source="ha_presence",
                person="alex_homeowner")
    assert r.resolution == "recorded"


def test_access_denied_always_recorded(conn):
    r = _signal(conn, signal="access_denied")
    assert r.resolution == "recorded"


# ---------------------------------------------------------------------------
# homeowner_count reflects latest signal per person
# ---------------------------------------------------------------------------

def test_homeowner_count_two_people_both_home(conn):
    _presence_home(conn, "alex_homeowner")
    _presence_home(conn, "sam_partner")
    as_of = datetime(2026, 5, 14, 10, 0, tzinfo=MEL)
    assert ha_signals._homeowner_count(conn, as_of, _settings()) == 2


def test_homeowner_count_one_away(conn):
    _presence_home(conn, "alex_homeowner",
                   dt=datetime(2026, 5, 14, 6, 0, tzinfo=MEL))
    _presence_away(conn, "alex_homeowner",
                   dt=datetime(2026, 5, 14, 8, 0, tzinfo=MEL))
    as_of = datetime(2026, 5, 14, 10, 0, tzinfo=MEL)
    assert ha_signals._homeowner_count(conn, as_of, _settings()) == 0


def test_homeowner_count_with_second_person_home(conn):
    _presence_home(conn, "alex_homeowner",
                   dt=datetime(2026, 5, 14, 6, 0, tzinfo=MEL))
    _presence_away(conn, "alex_homeowner",
                   dt=datetime(2026, 5, 14, 8, 0, tzinfo=MEL))
    _presence_home(conn, "sam_partner",
                   dt=datetime(2026, 5, 14, 9, 0, tzinfo=MEL))
    as_of = datetime(2026, 5, 14, 10, 0, tzinfo=MEL)
    assert ha_signals._homeowner_count(conn, as_of, _settings()) == 1


def test_homeowner_allowlist_ignores_unknown_person(conn):
    """A stray 'test' person (e.g. from a smoke test) is ignored at ingestion and
    never counts toward homeowner presence when an allowlist is configured."""
    allowed = _settings(homeowner_persons="alex_homeowner,sam_partner")
    res = _signal(conn, signal="homeowner_home", source="smoke",
                  person="test", settings=allowed)
    assert res.resolution == "ignored"

    # Even if such a row already existed in the DB, the count excludes it.
    conn.execute(
        "INSERT INTO ha_signals (occurred_at, source, signal, person)"
        " VALUES ('2026-05-14T00:00:00+10:00', 'smoke', 'homeowner_home', 'test')"
    )
    as_of = datetime(2026, 5, 14, 10, 0, tzinfo=MEL)
    assert ha_signals._homeowner_count(conn, as_of, allowed) == 0

    # A real, allowlisted homeowner still counts.
    _presence_home(conn, "alex_homeowner")
    assert ha_signals._homeowner_count(conn, as_of, allowed) == 1


def test_departure_watch_activates_when_second_homeowner_home(conn):
    """Departure watch uses count >= 1, so a second homeowner suffices."""
    nanny, _ = _seed(conn)
    _open_shift(conn, nanny)

    # Alex left before shift
    _presence_home(conn, "alex_homeowner",
                   dt=datetime(2026, 5, 14, 6, 0, tzinfo=MEL))
    _presence_away(conn, "alex_homeowner",
                   dt=datetime(2026, 5, 14, 6, 30, tzinfo=MEL))
    # Sam arrives after shift start
    _presence_home(conn, "sam_partner",
                   dt=datetime(2026, 5, 14, 17, 0, tzinfo=MEL))

    r = _signal(conn, signal="entry_pir", source="entry_pir",
                dt=datetime(2026, 5, 14, 17, 30, tzinfo=MEL))

    assert r.resolution == "departure"


# ---------------------------------------------------------------------------
# Signal DB row integrity
# ---------------------------------------------------------------------------

def test_signal_row_written_for_ignored(conn):
    nanny, _ = _seed(conn)
    r = _signal(conn, signal="access_granted")  # no homeowner → ignored
    row = conn.execute("SELECT * FROM ha_signals WHERE id=?", (r.signal_id,)).fetchone()
    assert row is not None
    assert row["resolution"] == "ignored"
    assert row["signal"] == "access_granted"


def test_signal_row_written_for_arrival(conn):
    nanny, hw = _seed(conn)
    _add_expected(conn, nanny)
    _presence_home(conn, hw)
    r = _signal(conn, signal="access_granted")
    row = conn.execute("SELECT * FROM ha_signals WHERE id=?", (r.signal_id,)).fetchone()
    assert row["resolution"] == "arrival"
    assert row["shift_id"] == r.shift_id
    assert row["nanny_id"] == nanny
