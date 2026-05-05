"""Home Assistant event resolution.

HA sends raw, unattributed presence events. We figure out which nanny it
belongs to by looking at the schedule (who was expected today?) and the
current state (any shifts open?). Output is a ``ha_events`` row with a
``resolution`` of arrival / departure / unresolved / ignored.

Resolution rules:

* ``event_type_hint`` from HA, if present, is treated as authoritative for
  *direction* — but we still need the schedule/state to attribute a nanny.
* Arrival: pick the nanny who is expected today and doesn't already have an
  open shift. If multiple or none → unresolved.
* Departure: pick the (single) currently-open shift. If multiple or none →
  unresolved.
* No hint: infer from state. 0 open shifts → arrival logic; 1 open and no
  other "expected but not arrived" nanny → departure; otherwise unresolved.
* Debounce: if a non-ignored event from the same ``source`` arrived within
  ``HA_DEBOUNCE_MINUTES``, the new one is recorded but resolved as ``ignored``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from shifter import repos, schedule
from shifter.config import Settings
from shifter.time_utils import ceil_15min, floor_15min


@dataclass
class EventResult:
    event_id: int
    resolution: str            # 'arrival' | 'departure' | 'unresolved' | 'ignored'
    nanny_id: int | None
    shift_id: int | None
    note: str | None


def _is_debounced(
    conn: sqlite3.Connection, source: str, occurred_at: datetime, window_minutes: int
) -> bool:
    if not source or window_minutes <= 0:
        return False
    cutoff = occurred_at - timedelta(minutes=window_minutes)
    row = conn.execute(
        "SELECT 1 FROM ha_events "
        " WHERE source = ? AND resolution != 'ignored'"
        "   AND occurred_at >= ? AND occurred_at < ?"
        " LIMIT 1",
        (source, cutoff.isoformat(), occurred_at.isoformat()),
    ).fetchone()
    return row is not None


def is_open_shift_stale(
    conn: sqlite3.Connection, shift, *, as_of: datetime, settings: Settings
) -> bool:
    """An open shift is stale and no longer accepts auto-attributed events if:
    (a) another shift has started after it (the next bucket has begun), or
    (c) it has been open for longer than ``shift_stale_hours`` (default 16h).

    Stale shifts stay open in the DB and are surfaced on the dashboard for
    manual close/edit; they're just invisible to the HA resolver.
    """
    start = datetime.fromisoformat(shift["start_time"])
    if (as_of - start).total_seconds() > settings.shift_stale_hours * 3600:
        return True
    later = conn.execute(
        "SELECT 1 FROM shifts WHERE start_time > ? LIMIT 1",
        (shift["start_time"],),
    ).fetchone()
    return later is not None


def _fresh_open_shifts(
    conn: sqlite3.Connection, *, as_of: datetime, settings: Settings
) -> list:
    return [s for s in repos.list_shifts(conn, open_only=True)
            if not is_open_shift_stale(conn, s, as_of=as_of, settings=settings)]


def _has_activity_today(
    conn: sqlite3.Connection, occurred_at: datetime, settings: Settings
) -> bool:
    """Is there any expected slot or *fresh* open shift the event could plausibly
    belong to? Stale open shifts don't count — they'd otherwise let stray
    morning events close yesterday's never-clocked-out shift.
    """
    if _fresh_open_shifts(conn, as_of=occurred_at, settings=settings):
        return True
    return bool(schedule.expected_on_date(conn, occurred_at.date(), include_cancelled=False))


def _try_arrival(
    conn: sqlite3.Connection, occurred_at: datetime, settings: Settings
):
    """Find an expected-but-not-arrived nanny on the event's date.
    A nanny with only a *stale* open shift counts as not-yet-arrived today,
    so consecutive-day work resolves cleanly."""
    on_date = occurred_at.date()
    expected = schedule.expected_on_date(conn, on_date, include_cancelled=False)
    if not expected:
        return None, None  # nobody expected
    fresh = _fresh_open_shifts(conn, as_of=occurred_at, settings=settings)
    open_nanny_ids = {s["nanny_id"] for s in fresh}
    not_arrived = [e for e in expected if e.nanny_id not in open_nanny_ids]
    distinct_nannies = {e.nanny_id for e in not_arrived}
    if len(distinct_nannies) == 1:
        slot = not_arrived[0]
        return slot.nanny_id, slot.expected_id
    return None, None


def _try_departure(
    conn: sqlite3.Connection, *, as_of: datetime, settings: Settings
):
    fresh = _fresh_open_shifts(conn, as_of=as_of, settings=settings)
    if len(fresh) == 1:
        return fresh[0]
    return None


def _insert_event(
    conn: sqlite3.Connection,
    *,
    occurred_at: datetime,
    source: str | None,
    event_type_hint: str | None,
    nanny_id: int | None,
    shift_id: int | None,
    expected_shift_id: int | None,
    resolution: str,
    note: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO ha_events ("
        " occurred_at, source, event_type_hint, nanny_id, shift_id,"
        " expected_shift_id, resolution, resolution_note"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            occurred_at.isoformat(), source, event_type_hint,
            nanny_id, shift_id, expected_shift_id, resolution, note,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _rounded_arrival_start(
    conn: sqlite3.Connection,
    *,
    occurred_at: datetime,
    expected_shift_id: int | None,
    nanny_id: int | None = None,
) -> datetime:
    """Round arrival time DOWN to the nearest 15 min, but never before the
    nanny's scheduled start for that day. If we have no expected_shift_id,
    fall back to looking up by (nanny_id, date)."""
    floored = floor_15min(occurred_at)
    expected = None
    if expected_shift_id is not None:
        expected = conn.execute(
            "SELECT start_time FROM expected_shifts WHERE id = ?",
            (expected_shift_id,),
        ).fetchone()
    elif nanny_id is not None:
        expected = conn.execute(
            "SELECT start_time FROM expected_shifts"
            " WHERE nanny_id = ? AND date = ? AND cancelled = 0"
            " ORDER BY start_time ASC LIMIT 1",
            (nanny_id, occurred_at.date().isoformat()),
        ).fetchone()
    if expected is None:
        return floored
    sched = datetime.combine(
        occurred_at.date(),
        time.fromisoformat(expected["start_time"]),
        tzinfo=occurred_at.tzinfo,
    )
    return max(floored, sched)


def _create_arrival(
    conn: sqlite3.Connection,
    *,
    nanny_id: int,
    expected_shift_id: int | None,
    occurred_at: datetime,
    source: str | None,
    event_type_hint: str | None,
) -> EventResult:
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
        created_by="ha-webhook",
    )
    eid = _insert_event(
        conn,
        occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
        nanny_id=nanny_id, shift_id=shift_id, expected_shift_id=expected_shift_id,
        resolution="arrival", note=None,
    )
    return EventResult(eid, "arrival", nanny_id, shift_id, None)


def _create_departure(
    conn: sqlite3.Connection,
    *,
    open_shift_row,
    occurred_at: datetime,
    source: str | None,
    event_type_hint: str | None,
) -> EventResult:
    end = ceil_15min(occurred_at)
    repos.close_shift(conn, open_shift_row["id"], end.isoformat(), updated_by="ha-webhook")
    eid = _insert_event(
        conn,
        occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
        nanny_id=open_shift_row["nanny_id"], shift_id=open_shift_row["id"],
        expected_shift_id=None, resolution="departure", note=None,
    )
    return EventResult(eid, "departure", open_shift_row["nanny_id"], open_shift_row["id"], None)


def process_event(
    conn: sqlite3.Connection,
    *,
    occurred_at: datetime,
    source: str | None,
    event_type_hint: str | None,
    settings: Settings,
) -> EventResult:
    """Resolve and persist a HA event. The conn must be in autocommit mode
    or the caller must wrap this in their own transaction.
    """
    # 1. Debounce
    if source and _is_debounced(conn, source, occurred_at, settings.ha_debounce_minutes):
        eid = _insert_event(
            conn,
            occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
            nanny_id=None, shift_id=None, expected_shift_id=None,
            resolution="ignored",
            note=f"debounced (within {settings.ha_debounce_minutes}min of previous {source} event)",
        )
        return EventResult(eid, "ignored", None, None,
                           f"debounced ({settings.ha_debounce_minutes}min)")

    def _record(resolution: str, note: str | None) -> EventResult:
        eid = _insert_event(
            conn,
            occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
            nanny_id=None, shift_id=None, expected_shift_id=None,
            resolution=resolution, note=note,
        )
        return EventResult(eid, resolution, None, None, note)

    # 2. Background-noise check: if nobody is scheduled today and no fresh
    # shift is open, an event almost certainly isn't a nanny — record as
    # ignored rather than burdening the unresolved queue.
    if not _has_activity_today(conn, occurred_at, settings):
        return _record("ignored", "no nanny scheduled today and no open shifts")

    # 3. Hint-driven resolution
    if event_type_hint == "arrival":
        nanny_id, exp_id = _try_arrival(conn, occurred_at, settings)
        if nanny_id:
            return _create_arrival(
                conn, nanny_id=nanny_id, expected_shift_id=exp_id,
                occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
            )
        return _record("unresolved", "hint=arrival but 0 or >1 candidate nannies on the schedule")

    if event_type_hint == "departure":
        open_shift = _try_departure(conn, as_of=occurred_at, settings=settings)
        if open_shift is not None:
            return _create_departure(
                conn, open_shift_row=open_shift,
                occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
            )
        return _record("unresolved", "hint=departure but 0 or >1 fresh open shifts")

    # 4. No hint — infer from state. Stale open shifts are excluded so a stray
    # morning event can't accidentally close yesterday's never-clocked-out
    # shift; that one stays open for manual cleanup. When an expected nanny
    # hasn't arrived yet today, we prefer the arrival interpretation — once
    # her new shift exists, the previous one becomes stale by rule (a) for
    # all future events.
    fresh_open = _fresh_open_shifts(conn, as_of=occurred_at, settings=settings)
    arrival_nanny_id, exp_id = _try_arrival(conn, occurred_at, settings)

    if arrival_nanny_id is not None:
        return _create_arrival(
            conn, nanny_id=arrival_nanny_id, expected_shift_id=exp_id,
            occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
        )
    if len(fresh_open) == 1:
        return _create_departure(
            conn, open_shift_row=fresh_open[0],
            occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
        )

    return _record(
        "unresolved",
        f"ambiguous: {len(fresh_open)} fresh open shift(s); no clear arrival candidate",
    )


# --- manual attribution of unresolved events ---------------------------------

def attribute_unresolved(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    nanny_id: int,
    direction: str,   # 'arrival' or 'departure'
    user: str,
) -> EventResult:
    event = conn.execute(
        "SELECT * FROM ha_events WHERE id = ?", (event_id,)
    ).fetchone()
    if event is None:
        raise ValueError(f"no event with id {event_id}")
    if event["resolution"] != "unresolved":
        raise ValueError(f"event {event_id} is already {event['resolution']}")

    occurred_at = datetime.fromisoformat(event["occurred_at"])

    if direction == "arrival":
        start = _rounded_arrival_start(
            conn, occurred_at=occurred_at, expected_shift_id=None, nanny_id=nanny_id,
        )
        shift_id = repos.create_shift(
            conn,
            nanny_id=nanny_id,
            start_time=start.isoformat(),
            end_time=None,
            rate_override_cents=None, flat_rate_cents=None, notes=None,
            source="ha", confirmed=False,
            created_by=f"manual:{user}",
        )
        conn.execute(
            "UPDATE ha_events SET resolution='arrival', nanny_id=?, shift_id=?,"
            " resolution_note='manually attributed' WHERE id = ?",
            (nanny_id, shift_id, event_id),
        )
        return EventResult(event_id, "arrival", nanny_id, shift_id, "manual")

    if direction == "departure":
        open_for_nanny = repos.list_shifts(conn, nanny_id=nanny_id, open_only=True)
        if not open_for_nanny:
            raise ValueError(f"nanny {nanny_id} has no open shift to close")
        shift = open_for_nanny[0]
        end = ceil_15min(occurred_at)
        repos.close_shift(conn, shift["id"], end.isoformat(), updated_by=f"manual:{user}")
        conn.execute(
            "UPDATE ha_events SET resolution='departure', nanny_id=?, shift_id=?,"
            " resolution_note='manually attributed' WHERE id = ?",
            (nanny_id, shift["id"], event_id),
        )
        return EventResult(event_id, "departure", nanny_id, shift["id"], "manual")

    raise ValueError(f"direction must be 'arrival' or 'departure', got {direction!r}")
