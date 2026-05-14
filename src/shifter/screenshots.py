"""Screenshot storage and retention.

Files live under ``settings.screenshot_dir/<YYYY>/<MM>/<event_id>_<rand>.<ext>``.
The DB row stores a *relative* path so the on-disk root can be moved freely.

Retention: a daily background task prunes files older than
``SCREENSHOT_RETENTION_DAYS``, but skips screenshots whose parent shift is unpaid.
The DB row is kept (with ``filename`` set to NULL) so the timeline still shows
"(screenshot expired)" rather than the row vanishing.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shifter.config import Settings

log = logging.getLogger("shifter.screenshots")

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def _write_screenshot(
    settings: Settings,
    *,
    prefix: str,
    content: bytes,
    content_type: str,
    taken_at: datetime,
) -> tuple[str, int]:
    ext = ALLOWED_CONTENT_TYPES.get(content_type)
    if ext is None:
        raise ValueError(f"Unsupported content type: {content_type}")
    rel_dir = Path(f"{taken_at.year:04d}") / f"{taken_at.month:02d}"
    abs_dir = settings.screenshot_dir / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}_{secrets.token_hex(4)}.{ext}"
    abs_path = abs_dir / name
    abs_path.write_bytes(content)
    return str(rel_dir / name), len(content)


def store_screenshot(
    *,
    settings: Settings,
    ha_event_id: int,
    content: bytes,
    content_type: str,
    taken_at: datetime,
) -> tuple[str, int]:
    """Write the file to disk; return (relative_path, size_bytes)."""
    return _write_screenshot(
        settings, prefix=str(ha_event_id), content=content,
        content_type=content_type, taken_at=taken_at,
    )


def store_signal_screenshot(
    *,
    settings: Settings,
    ha_signal_id: int,
    content: bytes,
    content_type: str,
    taken_at: datetime,
) -> tuple[str, int]:
    """Write a signal screenshot to disk; return (relative_path, size_bytes).
    Caller is responsible for updating ha_signals.snapshot_path in the DB.
    """
    return _write_screenshot(
        settings, prefix=f"sig{ha_signal_id}", content=content,
        content_type=content_type, taken_at=taken_at,
    )


def shots_for_shift(conn: sqlite3.Connection, shift_id: int) -> dict:
    """Return the earliest arrival snapshot and the latest departure snapshot
    for a shift.  Queries both the legacy ha_events/screenshots tables and the
    newer ha_signals table so both old and new shifts display correctly.
    """
    legacy = conn.execute(
        """
        SELECT s.filename, s.content_type, e.resolution, e.occurred_at
        FROM screenshots s
        JOIN ha_events e ON e.id = s.ha_event_id
        WHERE e.shift_id = ?
          AND s.filename IS NOT NULL
          AND e.resolution IN ('arrival', 'departure')
        ORDER BY e.occurred_at ASC, s.id ASC
        """,
        (shift_id,),
    ).fetchall()

    signals = conn.execute(
        """
        SELECT snapshot_path AS filename,
               'image/jpeg'  AS content_type,
               resolution,
               occurred_at
        FROM ha_signals
        WHERE shift_id = ?
          AND snapshot_path IS NOT NULL
          AND resolution IN ('arrival', 'departure')
        ORDER BY occurred_at ASC
        """,
        (shift_id,),
    ).fetchall()

    all_rows = sorted(
        [dict(r) for r in legacy] + [dict(r) for r in signals],
        key=lambda r: r["occurred_at"],
    )
    arrival = next((r for r in all_rows if r["resolution"] == "arrival"), None)
    departures = [r for r in all_rows if r["resolution"] == "departure"]
    departure = departures[-1] if departures else None
    return {
        "arrival": arrival,
        "departure": departure,
    }


def prune_expired(conn: sqlite3.Connection, settings: Settings, *, now: datetime | None = None) -> int:
    """Delete screenshot files older than retention, except those on unpaid shifts.

    Returns the number of files deleted.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=settings.screenshot_retention_days)
    rows = conn.execute(
        """
        SELECT s.id, s.filename
        FROM screenshots s
        JOIN ha_events e ON e.id = s.ha_event_id
        LEFT JOIN shifts sh ON sh.id = e.shift_id
        WHERE s.filename IS NOT NULL
          AND s.created_at < ?
          AND (sh.id IS NULL OR sh.paid_on IS NOT NULL)
        """,
        (cutoff.isoformat(sep=" ", timespec="seconds"),),
    ).fetchall()

    deleted = 0
    for row in rows:
        rel = row["filename"]
        abs_path = settings.screenshot_dir / rel
        try:
            abs_path.unlink(missing_ok=True)
            deleted += 1
        except OSError as e:
            log.warning("Failed to delete %s: %s", abs_path, e)
            continue
        conn.execute("UPDATE screenshots SET filename = NULL WHERE id = ?", (row["id"],))
    return deleted


async def cleanup_loop(conn: sqlite3.Connection, settings: Settings) -> None:
    """Background task: prune expired screenshots once per day."""
    while True:
        try:
            n = prune_expired(conn, settings)
            if n:
                log.info("Pruned %d expired screenshot(s)", n)
        except Exception:
            log.exception("Screenshot cleanup failed")
        await asyncio.sleep(24 * 3600)
