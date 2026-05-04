"""Money parsing & formatting. Keeps strings out of pay.py."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def parse_dollars(text: str) -> int:
    """Parse a user-entered dollar string ("38", "38.50", "$38.50") to cents.

    Raises ValueError on bad input or negative amounts.
    """
    s = text.strip().lstrip("$").replace(",", "")
    if not s:
        raise ValueError("amount is empty")
    try:
        d = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {text!r}") from exc
    if d < 0:
        raise ValueError("amount must be non-negative")
    cents = (d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(cents)


def format_cents(cents: int | None, *, prefix: str = "$") -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    cents_abs = abs(cents)
    dollars, c = divmod(cents_abs, 100)
    return f"{sign}{prefix}{dollars:,}.{c:02d}"


def format_cents_per_hour(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"{format_cents(cents)}/hr"
