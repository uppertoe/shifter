"""Home Assistant event resolution.

HA blindly fires presence events; the schedule + open-shift state filters
the noise. The resolver maps each event onto the shift state machine:

    A: Booked, awaiting arrival      (expected_shifts row, no shifts row yet)
    B: Open & fresh, awaiting departure  (shifts row, end_time NULL, < 16h old)
    C: Open & stale, resolution required (shifts row, end_time NULL, ≥ 16h old
                                          OR another shift opened after it)
    D: Closed, unconfirmed           (shifts row, end_time set, confirmed=0)
    E: Confirmed                     (shifts row, end_time set, confirmed=1)

Events either *consume* a shift's awaiting slot, *attach* to an existing
one, or are *ignored*:

* Arrival event matches state A → consume → creates a state-B shift.
* Arrival event with state A already consumed (auto-opener or earlier HA
  event opened the shift) → attach → event is recorded with shift_id so
  its snapshot surfaces on the dashboard card.
* Departure event matches a unique state-B shift → consume → closes to D.
* No-hint event: try arrival first, then fresh-departure.
* Anything else (no candidate, ambiguous candidates, stale shift, no
  schedule) → ``ignored``. There is no longer an "unresolved events"
  queue under this model — the schedule does the filtering, and shifts
  (states C and D) are the unit that needs human attention.

Every HA-driven close flips ``confirmed`` back to 0 on the shift, because
auto-filled end times are a convenience that always need human review
against the screenshot before they count. ALL shifts ultimately need
explicit confirmation; HA never confirms.

Debounce: if a non-ignored event from the same ``source`` arrived within
``HA_DEBOUNCE_MINUTES``, the new one is recorded but resolved as ``ignored``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from shifter import repos, schedule
from shifter.config import Settings
from shifter.time_utils import ceil_15min


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
    """An open shift is "stale" (state C) when we've clearly missed the close
    event for it:
    (a) another shift has started after it (the next bucket has begun), or
    (b) it has been open for longer than ``shift_stale_hours`` (default 16h).

    Stale shifts no longer accept HA events — that's the whole point of
    flagging them stale. They're surfaced on the dashboard for the human
    to set an end time (or delete) manually.
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
    """Open shifts in state B (not yet stale) — the only ones that can
    consume HA events."""
    return [s for s in repos.list_shifts(conn, open_only=True)
            if not is_open_shift_stale(conn, s, as_of=as_of, settings=settings)]


def _has_activity_today(
    conn: sqlite3.Connection, occurred_at: datetime, settings: Settings
) -> bool:
    """Is there any expected slot or fresh open shift the event could
    plausibly belong to? Stale open shifts don't count — they no longer
    accept events; events for them are background noise."""
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
    """Find the unique fresh (state B) open shift this departure can consume.
    Returns None on zero or multiple fresh open shifts (caller ignores)."""
    fresh = _fresh_open_shifts(conn, as_of=as_of, settings=settings)
    if len(fresh) == 1:
        return fresh[0]
    return None


def _try_attach_arrival(
    conn: sqlite3.Connection, occurred_at: datetime, settings: Settings
):
    """When an arrival event arrives *after* the shift was already opened
    (auto-opener fired first, or an earlier HA event), there's no new shift
    to create — but the event itself still carries useful context (e.g. a
    snapshot HA is about to upload). Attach the event to the existing shift
    so the snapshot surfaces on the shift's dashboard card.

    Conservative match: only attach when exactly one nanny is expected today
    and that nanny has exactly one fresh open shift. Anything ambiguous
    falls through to ``ignored`` — same philosophy as ``_try_arrival``.

    Returns (nanny_id, shift_id, expected_shift_id) or None.
    """
    on_date = occurred_at.date()
    expected = schedule.expected_on_date(conn, on_date, include_cancelled=False)
    distinct_nannies = {e.nanny_id for e in expected}
    if len(distinct_nannies) != 1:
        return None
    nanny_id = next(iter(distinct_nannies))
    fresh = _fresh_open_shifts(conn, as_of=occurred_at, settings=settings)
    open_for_nanny = [s for s in fresh if s["nanny_id"] == nanny_id]
    if len(open_for_nanny) != 1:
        return None
    slots_for_nanny = [e for e in expected if e.nanny_id == nanny_id]
    exp_id = slots_for_nanny[0].expected_id if len(slots_for_nanny) == 1 else None
    return nanny_id, open_for_nanny[0]["id"], exp_id


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
    """Round arrival time UP to the nearest 15 min (07:58 → 08:00), but never
    before the nanny's scheduled start for that day. Rounding up matches how
    late starts were being corrected by hand in prod (07:58 → 08:00,
    08:29 → 08:30), and mirrors the departure rule which also rounds up. If
    we have no expected_shift_id, fall back to looking up by (nanny_id, date)."""
    rounded = ceil_15min(occurred_at)
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
        return rounded
    sched = datetime.combine(
        occurred_at.date(),
        time.fromisoformat(expected["start_time"]),
        tzinfo=occurred_at.tzinfo,
    )
    return max(rounded, sched)


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
    # Force re-confirmation: an HA-driven close is a guess (especially the
    # stale-cleanup case where the end time is just "whenever the cleanup
    # event arrived"). Surface it on the dashboard for human review.
    conn.execute(
        "UPDATE shifts SET confirmed = 0 WHERE id = ?",
        (open_shift_row["id"],),
    )
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
    # shift is open, the event has nothing to match — drop it.
    if not _has_activity_today(conn, occurred_at, settings):
        return _record("ignored", "no nanny scheduled today and no fresh open shift")

    # 3. Hint-driven resolution. Misses are ignored — there's no longer an
    # unresolved-events queue under the shift-state model.
    if event_type_hint == "arrival":
        nanny_id, exp_id = _try_arrival(conn, occurred_at, settings)
        if nanny_id:
            return _create_arrival(
                conn, nanny_id=nanny_id, expected_shift_id=exp_id,
                occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
            )
        # No new shift to create — but if the shift already exists (auto-opener
        # fired, or a prior HA event), attach this event to it so the snapshot
        # links through. Shift start_time is left alone (the auto-opener used
        # the scheduled start; we don't want to retroactively shift it).
        attach = _try_attach_arrival(conn, occurred_at, settings)
        if attach is not None:
            n_id, s_id, e_id = attach
            eid = _insert_event(
                conn,
                occurred_at=occurred_at, source=source,
                event_type_hint=event_type_hint,
                nanny_id=n_id, shift_id=s_id, expected_shift_id=e_id,
                resolution="arrival",
                note="attached to existing open shift",
            )
            return EventResult(eid, "arrival", n_id, s_id, "attached")
        return _record("ignored", "hint=arrival but 0 or >1 candidate nannies on the schedule")

    if event_type_hint == "departure":
        open_shift = _try_departure(conn, as_of=occurred_at, settings=settings)
        if open_shift is not None:
            return _create_departure(
                conn, open_shift_row=open_shift,
                occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
            )
        return _record("ignored", "hint=departure but 0 or >1 fresh open shifts")

    # 4. No hint — infer from state. Prefer arrival (an expected nanny who
    # hasn't clocked in yet); else close the unique fresh open shift.
    # Stale shifts are intentionally invisible to the resolver — they need
    # human resolution.
    arrival_nanny_id, exp_id = _try_arrival(conn, occurred_at, settings)
    if arrival_nanny_id is not None:
        return _create_arrival(
            conn, nanny_id=arrival_nanny_id, expected_shift_id=exp_id,
            occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
        )

    open_shift = _try_departure(conn, as_of=occurred_at, settings=settings)
    if open_shift is not None:
        return _create_departure(
            conn, open_shift_row=open_shift,
            occurred_at=occurred_at, source=source, event_type_hint=event_type_hint,
        )

    fresh_open = _fresh_open_shifts(conn, as_of=occurred_at, settings=settings)
    return _record(
        "ignored",
        f"no clear match: {len(fresh_open)} fresh open shift(s),"
        " no arrival candidate",
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
