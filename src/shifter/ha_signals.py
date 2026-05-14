"""Home Assistant signal processing — signal-based architecture.

HA sends raw, typed signals; Shifter resolves them lazily against DB state.
No boolean flags or timers live in HA.

Signal types
------------
access_granted      Rosslare keypad code accepted OR app unlock
access_denied       Rosslare keypad code rejected
entry_pir           Entry-zone PIR fired
front_deck_person   Frigate person detected on front deck
verandah_person     Frigate person detected on verandah
homeowner_home      Tracked person arrived home (person= required)
homeowner_away      Tracked person left home (person= required)

Arrival path (access_granted)
------------------------------
1. homeowner_count >= 1               (someone is home to confirm the nanny)
2. No fresh open shift                (nanny not already clocked in)
3. Expected shift within ±pre_shift_window_minutes of occurred_at
→ open shift (resolution = 'arrival')

Failed-entry arrival fallback (entry_pir only)
-----------------------------------------------
1. Any access_denied within failed_entry_window_minutes
2. No fresh open shift
3. homeowner_count >= 1
4. Expected shift within window
→ open shift with source='ha-failed-entry' (resolution = 'arrival')

Departure path (entry_pir | front_deck_person | verandah_person)
----------------------------------------------------------------
departure_watch_active when ALL of:
  • Exactly one fresh open shift
  • homeowner_count >= 1 (as-of occurred_at)
  • Any homeowner_home signal after shift.start_time

AND signal is NOT in the entry-suppression window
  (access_granted within entry_suppression_minutes)

→ close shift (resolution = 'departure')

Presence + access_denied signals are always recorded (no action).
Signals that pass no relevant condition are 'ignored'.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from shifter import repos, schedule
from shifter.config import Settings
from shifter.ha import _fresh_open_shifts, _rounded_arrival_start  # shared helpers
from shifter.time_utils import ceil_15min


VALID_SIGNALS = frozenset({
    "access_granted",
    "access_denied",
    "entry_pir",
    "front_deck_person",
    "verandah_person",
    "homeowner_home",
    "homeowner_away",
})

_PRESENCE_SIGNALS = frozenset({"homeowner_home", "homeowner_away"})
_PHYSICAL_SIGNALS = frozenset({"entry_pir", "front_deck_person", "verandah_person"})


@dataclass
class SignalResult:
    signal_id: int
    resolution: str  # 'arrival' | 'departure' | 'recorded' | 'ignored'
    nanny_id: int | None
    shift_id: int | None
    note: str | None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _insert_signal(
    conn: sqlite3.Connection,
    *,
    occurred_at: datetime,
    source: str,
    signal: str,
    person: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO ha_signals (occurred_at, source, signal, person)"
        " VALUES (?, ?, ?, ?)",
        (occurred_at.isoformat(), source, signal, person),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _resolve(
    conn: sqlite3.Connection,
    signal_id: int,
    *,
    resolution: str,
    note: str | None = None,
    nanny_id: int | None = None,
    shift_id: int | None = None,
) -> None:
    conn.execute(
        "UPDATE ha_signals SET resolution=?, resolution_note=?, nanny_id=?, shift_id=?"
        " WHERE id=?",
        (resolution, note, nanny_id, shift_id, signal_id),
    )


# ---------------------------------------------------------------------------
# State derivation (lazy, no HA-side flags)
# ---------------------------------------------------------------------------

def _homeowner_count(conn: sqlite3.Connection, as_of: datetime) -> int:
    """Count persons whose latest presence signal (≤ as_of) is homeowner_home."""
    persons = conn.execute(
        "SELECT DISTINCT person FROM ha_signals"
        " WHERE signal IN ('homeowner_home','homeowner_away') AND person IS NOT NULL"
    ).fetchall()
    count = 0
    for row in persons:
        latest = conn.execute(
            "SELECT signal FROM ha_signals"
            " WHERE person = ? AND signal IN ('homeowner_home','homeowner_away')"
            "   AND occurred_at <= ?"
            " ORDER BY occurred_at DESC LIMIT 1",
            (row["person"], as_of.isoformat()),
        ).fetchone()
        if latest and latest["signal"] == "homeowner_home":
            count += 1
    return count


def _departure_watch_active(
    conn: sqlite3.Connection, *, as_of: datetime, settings: Settings
) -> tuple[bool, object]:
    """(active, open_shift_row | None).

    Active when: exactly one fresh open shift, homeowner present, and at least
    one homeowner_home signal arrived *after* the shift started.
    """
    fresh = _fresh_open_shifts(conn, as_of=as_of, settings=settings)
    if len(fresh) != 1:
        return False, None
    shift = fresh[0]

    if _homeowner_count(conn, as_of) < 1:
        return False, shift

    row = conn.execute(
        "SELECT 1 FROM ha_signals"
        " WHERE signal = 'homeowner_home' AND occurred_at > ? LIMIT 1",
        (shift["start_time"],),
    ).fetchone()
    return (row is not None), shift


def _entry_suppressed(
    conn: sqlite3.Connection, occurred_at: datetime, settings: Settings
) -> bool:
    """True if access_granted arrived within entry_suppression_minutes of occurred_at."""
    if settings.entry_suppression_minutes <= 0:
        return False
    cutoff = (occurred_at - timedelta(minutes=settings.entry_suppression_minutes)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM ha_signals WHERE signal = 'access_granted'"
        " AND occurred_at >= ? AND occurred_at <= ? LIMIT 1",
        (cutoff, occurred_at.isoformat()),
    ).fetchone()
    return row is not None


def _failed_entry_recent(
    conn: sqlite3.Connection, occurred_at: datetime, settings: Settings
) -> bool:
    """True if access_denied arrived within failed_entry_window_minutes."""
    cutoff = (occurred_at - timedelta(minutes=settings.failed_entry_window_minutes)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM ha_signals WHERE signal = 'access_denied'"
        " AND occurred_at >= ? AND occurred_at <= ? LIMIT 1",
        (cutoff, occurred_at.isoformat()),
    ).fetchone()
    return row is not None


def _expected_in_window(
    conn: sqlite3.Connection, occurred_at: datetime, settings: Settings
) -> tuple[int | None, int | None]:
    """(nanny_id, expected_shift_id) when exactly one nanny has a scheduled shift
    within ±pre_shift_window_minutes of occurred_at.  Returns (None, None) otherwise.
    """
    window = timedelta(minutes=settings.pre_shift_window_minutes)
    on_date = occurred_at.date()
    expected = schedule.expected_on_date(conn, on_date, include_cancelled=False)
    if not expected:
        return None, None

    in_window = []
    for slot in expected:
        sched_start = datetime.combine(
            on_date,
            time.fromisoformat(slot.start_time),
            tzinfo=occurred_at.tzinfo,
        )
        if abs((occurred_at - sched_start).total_seconds()) <= window.total_seconds():
            in_window.append(slot)

    distinct_nannies = {s.nanny_id for s in in_window}
    if len(distinct_nannies) != 1:
        return None, None
    slot = in_window[0]
    return slot.nanny_id, slot.expected_id


# ---------------------------------------------------------------------------
# Resolution actions
# ---------------------------------------------------------------------------

def _do_arrival(
    conn: sqlite3.Connection,
    signal_id: int,
    *,
    nanny_id: int,
    expected_shift_id: int | None,
    occurred_at: datetime,
    source: str,
) -> SignalResult:
    start = _rounded_arrival_start(
        conn, occurred_at=occurred_at, expected_shift_id=expected_shift_id,
    )
    shift_id = repos.create_shift(
        conn,
        nanny_id=nanny_id,
        start_time=start.isoformat(),
        end_time=None,
        rate_override_cents=None,
        flat_rate_cents=None,
        notes=None,
        source="ha",
        confirmed=False,
        created_by="ha-signals",
    )
    _resolve(conn, signal_id, resolution="arrival", nanny_id=nanny_id, shift_id=shift_id)
    return SignalResult(signal_id, "arrival", nanny_id, shift_id, None)


def _do_departure(
    conn: sqlite3.Connection,
    signal_id: int,
    *,
    open_shift,
    occurred_at: datetime,
) -> SignalResult:
    end = ceil_15min(occurred_at)
    repos.close_shift(conn, open_shift["id"], end.isoformat(), updated_by="ha-signals")
    conn.execute("UPDATE shifts SET confirmed = 0 WHERE id = ?", (open_shift["id"],))
    _resolve(
        conn, signal_id,
        resolution="departure",
        nanny_id=open_shift["nanny_id"],
        shift_id=open_shift["id"],
    )
    return SignalResult(signal_id, "departure", open_shift["nanny_id"], open_shift["id"], None)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_signal(
    conn: sqlite3.Connection,
    *,
    occurred_at: datetime,
    source: str,
    signal: str,
    person: str | None,
    settings: Settings,
) -> SignalResult:
    """Insert and resolve a single HA signal.  conn must be in autocommit mode."""
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=settings.zoneinfo)

    signal_id = _insert_signal(
        conn, occurred_at=occurred_at, source=source, signal=signal, person=person,
    )

    def _rec(note: str | None = None) -> SignalResult:
        _resolve(conn, signal_id, resolution="recorded", note=note)
        return SignalResult(signal_id, "recorded", None, None, note)

    def _ign(note: str) -> SignalResult:
        _resolve(conn, signal_id, resolution="ignored", note=note)
        return SignalResult(signal_id, "ignored", None, None, note)

    # ── Presence signals: always recorded, no action ─────────────────────────
    if signal in _PRESENCE_SIGNALS:
        return _rec()

    # ── access_denied: record for failed-entry fallback window ───────────────
    if signal == "access_denied":
        return _rec()

    # ── access_granted: potential arrival ────────────────────────────────────
    if signal == "access_granted":
        fresh = _fresh_open_shifts(conn, as_of=occurred_at, settings=settings)
        if fresh:
            return _rec("open shift exists; homeowner return or duplicate")

        if _homeowner_count(conn, occurred_at) < 1:
            return _ign("no homeowner home; cannot confirm nanny arrival")

        nanny_id, exp_id = _expected_in_window(conn, occurred_at, settings)
        if nanny_id is None:
            return _ign("no expected nanny within pre_shift_window_minutes")

        return _do_arrival(
            conn, signal_id,
            nanny_id=nanny_id, expected_shift_id=exp_id,
            occurred_at=occurred_at, source=source,
        )

    # ── Physical signals: departure or failed-entry arrival ──────────────────
    if signal in _PHYSICAL_SIGNALS:
        watch_active, open_shift = _departure_watch_active(
            conn, as_of=occurred_at, settings=settings,
        )

        if watch_active:
            if _entry_suppressed(conn, occurred_at, settings):
                return _rec("departure suppressed — access_granted within suppression window")
            return _do_departure(conn, signal_id, open_shift=open_shift, occurred_at=occurred_at)

        # Failed-entry fallback — only for entry_pir (PIR is direction-aware;
        # Frigate outdoor cameras can't confirm someone was let inside).
        if signal == "entry_pir" and _failed_entry_recent(conn, occurred_at, settings):
            fresh = _fresh_open_shifts(conn, as_of=occurred_at, settings=settings)
            if not fresh and _homeowner_count(conn, occurred_at) >= 1:
                nanny_id, exp_id = _expected_in_window(conn, occurred_at, settings)
                if nanny_id is not None:
                    return _do_arrival(
                        conn, signal_id,
                        nanny_id=nanny_id, expected_shift_id=exp_id,
                        occurred_at=occurred_at, source="ha-failed-entry",
                    )

        return _rec(f"{signal}: departure watch not active")

    return _ign(f"unknown signal: {signal!r}")
