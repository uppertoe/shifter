"""Schedule patterns and materialised expected_shifts.

A *pattern* is a recurring weekly slot ("Joy works Wednesdays 7:00–18:00 from
2026-05-01"). The materialiser projects active patterns into concrete
``expected_shifts`` rows so the calendar UI can render fast and the HA event
resolver can ask "who was expected today?".

Manual (one-off) rows are inserted directly into ``expected_shifts`` with
``source='manual'``. The UNIQUE(nanny, date, start_time) constraint stops the
materialiser from clobbering them on the next run.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("shifter.schedule")

PROJECTION_WEEKS_AHEAD = 12


@dataclass
class ExpectedSlot:
    expected_id: int
    nanny_id: int
    nanny_name: str
    date: date
    start_time: str  # 'HH:MM'
    end_time: str
    source: str
    cancelled: bool
    pattern_id: int | None


def list_patterns(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, n.name AS nanny_name "
        "FROM schedule_patterns p JOIN nannies n ON n.id = p.nanny_id "
        "ORDER BY n.name, p.day_of_week, p.start_time"
    ).fetchall()


def create_pattern(
    conn: sqlite3.Connection,
    *,
    nanny_id: int,
    day_of_week: int,
    start_time: str,
    end_time: str,
    active_from: date,
    active_until: date | None = None,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO schedule_patterns (nanny_id, day_of_week, start_time, end_time,"
        " active_from, active_until, notes) VALUES (?,?,?,?,?,?,?)",
        (nanny_id, day_of_week, start_time, end_time,
         active_from.isoformat(),
         active_until.isoformat() if active_until else None,
         notes),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def delete_pattern(conn: sqlite3.Connection, pattern_id: int, *, also_remove_unused_expected: bool = True) -> None:
    if also_remove_unused_expected:
        # Drop pattern-generated future rows that aren't cancelled or in the past.
        conn.execute(
            "DELETE FROM expected_shifts "
            "WHERE pattern_id = ? AND cancelled = 0 AND date >= date('now')",
            (pattern_id,),
        )
    conn.execute("DELETE FROM schedule_patterns WHERE id = ?", (pattern_id,))


def materialize(
    conn: sqlite3.Connection,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> int:
    """Project active patterns into expected_shifts. Returns rows inserted.

    Idempotent: the UNIQUE constraint and INSERT OR IGNORE prevent duplicates.
    Pattern slots whose date already has an expected_shifts row (manual or
    cancelled) are left untouched.
    """
    today = date.today()
    start = from_date or today
    end = to_date or (today + timedelta(weeks=PROJECTION_WEEKS_AHEAD))
    patterns = conn.execute(
        "SELECT * FROM schedule_patterns WHERE active_from <= ?",
        (end.isoformat(),),
    ).fetchall()
    if not patterns:
        return 0

    inserted = 0
    for p in patterns:
        p_from = max(start, date.fromisoformat(p["active_from"]))
        p_to = end
        if p["active_until"]:
            p_to = min(end, date.fromisoformat(p["active_until"]))
        if p_from > p_to:
            continue
        # Iterate through dates and insert where day_of_week matches.
        cur = p_from
        # Snap forward to the right day_of_week.
        delta = (p["day_of_week"] - cur.weekday()) % 7
        cur = cur + timedelta(days=delta)
        while cur <= p_to:
            rc = conn.execute(
                "INSERT OR IGNORE INTO expected_shifts "
                " (nanny_id, date, start_time, end_time, pattern_id, source) "
                " VALUES (?, ?, ?, ?, ?, 'pattern')",
                (p["nanny_id"], cur.isoformat(), p["start_time"], p["end_time"], p["id"]),
            )
            inserted += rc.rowcount
            cur = cur + timedelta(days=7)
    return inserted


def add_one_off(
    conn: sqlite3.Connection,
    *,
    nanny_id: int,
    on_date: date,
    start_time: str,
    end_time: str,
    notes: str | None = None,
) -> int:
    """Add a manual expected shift, or un-cancel an existing matching slot.

    If a row exists with the same (nanny, date, start_time), this only un-cancels
    it — source is preserved (a pattern row stays a pattern row, not silently
    promoted to manual).
    """
    existing = conn.execute(
        "SELECT id FROM expected_shifts "
        " WHERE nanny_id = ? AND date = ? AND start_time = ?",
        (nanny_id, on_date.isoformat(), start_time),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE expected_shifts SET cancelled = 0 WHERE id = ?",
            (existing["id"],),
        )
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO expected_shifts (nanny_id, date, start_time, end_time, source, notes)"
        " VALUES (?, ?, ?, ?, 'manual', ?)",
        (nanny_id, on_date.isoformat(), start_time, end_time, notes),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def cancel_expected(conn: sqlite3.Connection, expected_id: int) -> None:
    """Soft-cancel an expected shift (preserves the row for history)."""
    conn.execute("UPDATE expected_shifts SET cancelled = 1 WHERE id = ?", (expected_id,))


def uncancel_expected(conn: sqlite3.Connection, expected_id: int) -> None:
    conn.execute("UPDATE expected_shifts SET cancelled = 0 WHERE id = ?", (expected_id,))


def delete_expected(conn: sqlite3.Connection, expected_id: int) -> None:
    """Hard-delete (used for cleaning up manual one-offs added by mistake)."""
    conn.execute("DELETE FROM expected_shifts WHERE id = ?", (expected_id,))


def expected_in_range(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    include_cancelled: bool = True,
) -> list[ExpectedSlot]:
    sql = (
        "SELECT e.*, n.name AS nanny_name "
        " FROM expected_shifts e JOIN nannies n ON n.id = e.nanny_id "
        " WHERE e.date >= ? AND e.date < ?"
    )
    if not include_cancelled:
        sql += " AND e.cancelled = 0"
    sql += " ORDER BY e.date, e.start_time"
    rows = conn.execute(sql, (start.isoformat(), end.isoformat())).fetchall()
    return [
        ExpectedSlot(
            expected_id=r["id"],
            nanny_id=r["nanny_id"],
            nanny_name=r["nanny_name"],
            date=date.fromisoformat(r["date"]),
            start_time=r["start_time"],
            end_time=r["end_time"],
            source=r["source"],
            cancelled=bool(r["cancelled"]),
            pattern_id=r["pattern_id"],
        )
        for r in rows
    ]


def expected_on_date(
    conn: sqlite3.Connection, on_date: date, *, include_cancelled: bool = False
) -> list[ExpectedSlot]:
    return expected_in_range(
        conn, start=on_date, end=on_date + timedelta(days=1),
        include_cancelled=include_cancelled,
    )


# --- auto-opener -------------------------------------------------------------

def slot_window(slot: ExpectedSlot, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Concrete (start_dt, end_dt) for an expected slot. End wraps to the next
    day when end_time <= start_time (overnight shift)."""
    start_t = time.fromisoformat(slot.start_time)
    end_t = time.fromisoformat(slot.end_time)
    start_dt = datetime.combine(slot.date, start_t, tzinfo=tz)
    end_date = slot.date + timedelta(days=1) if end_t <= start_t else slot.date
    end_dt = datetime.combine(end_date, end_t, tzinfo=tz)
    return start_dt, end_dt


def _slot_opened(conn: sqlite3.Connection, slot: ExpectedSlot, *, tz: ZoneInfo) -> bool:
    """True iff a shifts row already covers this expected slot. Overlap check:
    the shift starts before the slot ends and either is still open or ended
    after the slot started."""
    start_dt, end_dt = slot_window(slot, tz)
    row = conn.execute(
        "SELECT 1 FROM shifts "
        " WHERE nanny_id = ?"
        "   AND start_time < ?"
        "   AND (end_time IS NULL OR end_time > ?)"
        " LIMIT 1",
        (slot.nanny_id, end_dt.isoformat(), start_dt.isoformat()),
    ).fetchone()
    return row is not None


def pending_for_date(
    conn: sqlite3.Connection, on_date: date, *, tz: ZoneInfo
) -> list[ExpectedSlot]:
    """Today's expected slots that haven't been matched by a shift yet —
    the queue the dashboard shows so the human can adjust times, open early,
    or cancel before the auto-opener fires."""
    return [
        s for s in expected_on_date(conn, on_date, include_cancelled=False)
        if not _slot_opened(conn, s, tz=tz)
    ]


def auto_open_due_at(slot: ExpectedSlot, tz: ZoneInfo, settings) -> datetime:
    """When the auto-opener is allowed to open a shift for `slot` if no HA
    arrival has been detected by then.

    Arrival detection (ha_signals: keypad access_granted within
    ±pre_shift_window_minutes of the scheduled start) is the preferred way to
    open a shift because it records the *actual* arrival time. The auto-opener
    is only the fallback for the shifts where nobody used the keypad (the nanny
    was let in, the door was already open, ...). So it waits until the arrival
    window has closed — scheduled start + pre_shift_window_minutes — before it
    pins the shift at the scheduled start. Capped at half the slot length so a
    short slot still gets opened while it is in progress.
    """
    start_dt, end_dt = slot_window(slot, tz)
    grace = timedelta(minutes=settings.pre_shift_window_minutes)
    half = (end_dt - start_dt) / 2
    return start_dt + min(grace, half)


def auto_open_due(
    conn: sqlite3.Connection, *, now: datetime, settings
) -> list[int]:
    """Open shifts for any expected slot whose arrival window has closed (see
    auto_open_due_at), whose scheduled window is still in progress, and that
    doesn't already have a matching shift. Returns the ids of any shifts
    created.

    Scans today AND yesterday so overnight shifts (e.g. 19:00 Mon → 06:00 Tue)
    get opened correctly if the app restarts mid-shift.

    The created shift starts at the scheduled start, not at `now` — by the
    time the fallback fires we have no better information than the schedule,
    and the shift is left confirmed=0 so a human reviews it (same as
    HA-sourced shifts).
    """
    from shifter import repos  # local import to avoid module cycle

    tz = settings.zoneinfo
    today = now.astimezone(tz).date()
    created: list[int] = []
    for d in (today - timedelta(days=1), today):
        for slot in expected_on_date(conn, d, include_cancelled=False):
            start_dt, end_dt = slot_window(slot, tz)
            if end_dt <= now or auto_open_due_at(slot, tz, settings) > now:
                continue
            if _slot_opened(conn, slot, tz=tz):
                continue
            sid = repos.create_shift(
                conn,
                nanny_id=slot.nanny_id,
                start_time=start_dt.isoformat(),
                end_time=None,
                rate_override_cents=None,
                flat_rate_cents=None,
                notes=None,
                source="auto",
                confirmed=False,
                created_by="auto-opener",
            )
            created.append(sid)
    return created


def update_expected_times(
    conn: sqlite3.Connection, expected_id: int, *,
    start_time: str, end_time: str,
) -> None:
    """One-off override of an expected_shifts row's times (e.g. today's slot
    runs 07:30–18:30 instead of the pattern's 07:00–18:00). Does not touch
    the underlying schedule_pattern."""
    conn.execute(
        "UPDATE expected_shifts SET start_time = ?, end_time = ? WHERE id = ?",
        (start_time, end_time, expected_id),
    )


async def auto_open_loop(
    conn: sqlite3.Connection, settings, *, interval_seconds: int = 60
) -> None:
    """Background task: check every interval for expected slots whose start
    time has passed and open a shift for each. Cheap to run; one SELECT per
    pending slot."""
    while True:
        try:
            now = datetime.now(settings.zoneinfo)
            created = auto_open_due(conn, now=now, settings=settings)
            if created:
                log.info("Auto-opened %d scheduled shift(s): %s",
                         len(created), created)
        except Exception:
            log.exception("Auto-open loop failed")
        await asyncio.sleep(interval_seconds)
