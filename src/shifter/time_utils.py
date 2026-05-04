"""Time helpers. All app-internal datetimes are tz-aware ISO 8601."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def parse_local_input(s: str, tz: ZoneInfo) -> datetime:
    """Parse an HTML <input type=datetime-local> value ('YYYY-MM-DDTHH:MM') in `tz`."""
    return datetime.fromisoformat(s).replace(tzinfo=tz)


def to_local_input(iso: str, tz: ZoneInfo) -> str:
    """ISO+offset → 'YYYY-MM-DDTHH:MM' in `tz` for prefilling form inputs."""
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def format_dt(iso: str | None, tz: ZoneInfo) -> str:
    """Human-friendly local-time display: 'Mon 4 May, 7:00am'."""
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso).astimezone(tz)
    # Use platform-portable day/hour formatting (avoid %-d which is GNU only).
    day = dt.day
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{dt.strftime('%a')} {day} {dt.strftime('%b')}, {hour12}:{dt.minute:02d}{ampm}"


def format_date(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso).astimezone(tz)
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b %Y')}"


def format_duration(start_iso: str, end_iso: str | None, *, now: datetime | None = None) -> str:
    """e.g. '8h 30m' or '8h 30m (open)' if end is None."""
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso) if end_iso else (now or datetime.now(start.tzinfo))
    delta: timedelta = end - start
    if delta.total_seconds() < 0:
        return "0m"
    total_min = int(delta.total_seconds() // 60)
    h, m = divmod(total_min, 60)
    base = f"{h}h {m}m" if h else f"{m}m"
    return base + (" (open)" if end_iso is None else "")
