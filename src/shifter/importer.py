"""TimeTagger CSV importer.

Format (tab-separated, header row present):
    key    start    stop    tags    description

* `start` and `stop` accept either:
    - ISO 8601 with offset (e.g. ``2025-04-01T07:00:00Z`` or ``…+10:00``) —
      converted to the configured timezone.
    - Naive local timestamps (``YYYY-MM-DD HH:MM:SS``) — interpreted as
      already in the configured timezone.
  TimeTagger's own export format uses the ISO 8601 ``Z``-suffixed form.
* `tags` is a whitespace-separated list of `#tag` words (may be empty).
* `description` is free text. May contain expense hints like "lunch $12.90".

Behaviour:
* Filters rows: only imports those tagged with one of the user-mapped nanny tags.
* Idempotent via the UNIQUE `shifts.timetagger_key` index.
* Imports as paid (configurable) — historical shifts are presumably already paid.
* Auto-extracts expense *candidates* from descriptions and inserts them with
  ``pending_review = 1`` so the user can vet them in the UI before they're
  marked paid.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

# Match patterns ordered most-specific → most-general. First match wins per row.
# Each pattern must yield two groups in this conceptual order: (word, amount).
# Dollar-prefixed and ".dd" decimals reduce false positives on times like "07:30".
EXPENSE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "Add $12 for lunch", "$53 for aquarium" — amount, then 'for', then word
    (re.compile(r"(?:add\s+)?\$(\d+(?:\.\d{1,2})?)\s+for\s+(\w+)", re.I), "amount_then_word"),
    # "lunch $10", "+ lunch $14"
    (re.compile(r"(\w+)\s+\$(\d+(?:\.\d{1,2})?)", re.I), "word_then_amount"),
    # "lunch 46.50" — explicit decimal, no $ sign
    (re.compile(r"(\w+)\s+(\d+\.\d{2})\b"), "word_then_amount"),
]

NOT_WORKED_RE = re.compile(r"\bnot\s*worked\b|sick-not-worked", re.I)


@dataclass
class ExtractedExpense:
    description: str
    amount_cents: int


@dataclass
class ParsedRow:
    key: str
    start_local: datetime  # tz-aware
    end_local: datetime    # tz-aware
    tags: list[str]        # without leading '#'
    description: str
    expense_candidates: list[ExtractedExpense] = field(default_factory=list)
    not_worked_note: bool = False


@dataclass
class ImportPlan:
    """A view of what would happen if we ran the import. Use --dry-run to inspect."""
    file: Path
    rows_total: int
    rows_skipped_no_mapping: int
    rows_skipped_already_imported: int
    rows_skipped_empty: int
    by_nanny_id: dict[int, int]
    skipped_unique_tags: Counter
    expense_candidates_total: int
    not_worked_count: int


@dataclass
class ImportResult:
    shifts_inserted: int
    expenses_inserted: int
    rows_skipped_no_mapping: int
    rows_skipped_already_imported: int
    rows_skipped_empty: int


def parse_amount_to_cents(s: str) -> int:
    # Always two-decimal interpretation: "10" → 1000 (ten dollars), "10.5" → 1050.
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "00")[:2]
        return int(whole) * 100 + int(frac)
    return int(s) * 100


def extract_expenses(description: str) -> list[ExtractedExpense]:
    """Find dollar-amount candidates in a TimeTagger description.

    Returns deduped expenses (by amount). Conservative — leaves it to the user
    to verify in the UI rather than silently adding mystery expenses.
    """
    out: list[ExtractedExpense] = []
    seen_amounts: set[int] = set()
    for pattern, kind in EXPENSE_PATTERNS:
        for m in pattern.finditer(description):
            if kind == "amount_then_word":
                amount_str, word = m.group(1), m.group(2)
            else:  # word_then_amount
                word, amount_str = m.group(1), m.group(2)
            cents = parse_amount_to_cents(amount_str)
            if cents == 0 or cents in seen_amounts:
                continue
            seen_amounts.add(cents)
            out.append(ExtractedExpense(description=word.lower(), amount_cents=cents))
    return out


def parse_tags(tags_field: str) -> list[str]:
    return [t.lstrip("#").strip() for t in tags_field.split() if t.startswith("#")]


def _parse_timestamp(s: str, tz: ZoneInfo) -> datetime | None:
    """Accept ISO 8601 (with ``Z`` or numeric offset) or the legacy naive
    ``YYYY-MM-DD HH:MM:SS`` form. Returns a tz-aware datetime in ``tz`` or
    None if neither format parses."""
    # ISO 8601 first — TimeTagger's actual export format. fromisoformat in
    # Python 3.11+ accepts the "Z" suffix directly.
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def parse_file(path: Path, tz: ZoneInfo) -> Iterable[ParsedRow]:
    """Yield ParsedRow for every non-empty data line in the TSV."""
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            key = (row.get("key") or "").strip()
            start_str = (row.get("start") or "").strip()
            stop_str = (row.get("stop") or "").strip()
            if not key or not start_str or not stop_str:
                continue
            start = _parse_timestamp(start_str, tz)
            end = _parse_timestamp(stop_str, tz)
            if start is None or end is None:
                continue  # malformed — skip
            tags = parse_tags(row.get("tags") or "")
            description = (row.get("description") or "").strip()
            yield ParsedRow(
                key=key,
                start_local=start,
                end_local=end,
                tags=tags,
                description=description,
                expense_candidates=extract_expenses(description),
                not_worked_note=bool(NOT_WORKED_RE.search(description) or NOT_WORKED_RE.search(" ".join(tags))),
            )


def plan_import(
    conn: sqlite3.Connection,
    path: Path,
    tag_to_nanny_id: dict[str, int],
    *,
    tz: ZoneInfo,
) -> ImportPlan:
    rows_total = 0
    by_nanny: dict[int, int] = {}
    skipped_no_mapping = 0
    skipped_already = 0
    skipped_empty = 0
    skipped_tags: Counter = Counter()
    exp_total = 0
    not_worked = 0

    existing_keys = {
        r["timetagger_key"]
        for r in conn.execute(
            "SELECT timetagger_key FROM shifts WHERE timetagger_key IS NOT NULL"
        )
    }

    for parsed in parse_file(path, tz):
        rows_total += 1
        nid = _resolve_nanny(parsed.tags, tag_to_nanny_id)
        if nid is None:
            if not parsed.tags:
                skipped_empty += 1
            else:
                skipped_no_mapping += 1
                # Tally the *first* unmapped tag to surface the most common
                # category for the user.
                for t in parsed.tags:
                    if t.lower() not in tag_to_nanny_id:
                        skipped_tags[t.lower()] += 1
                        break
            continue
        if parsed.key in existing_keys:
            skipped_already += 1
            continue
        by_nanny[nid] = by_nanny.get(nid, 0) + 1
        exp_total += len(parsed.expense_candidates)
        if parsed.not_worked_note:
            not_worked += 1

    return ImportPlan(
        file=path,
        rows_total=rows_total,
        rows_skipped_no_mapping=skipped_no_mapping,
        rows_skipped_already_imported=skipped_already,
        rows_skipped_empty=skipped_empty,
        by_nanny_id=by_nanny,
        skipped_unique_tags=skipped_tags,
        expense_candidates_total=exp_total,
        not_worked_count=not_worked,
    )


def run_import(
    conn: sqlite3.Connection,
    path: Path,
    tag_to_nanny_id: dict[str, int],
    *,
    tz: ZoneInfo,
    paid_on: date | None,
    created_by: str = "import",
) -> ImportResult:
    """Insert shifts + expense candidates. Returns counts.

    Idempotent via shifts.timetagger_key UNIQUE constraint.
    """
    shifts_inserted = 0
    expenses_inserted = 0
    skipped_no_mapping = 0
    skipped_already = 0
    skipped_empty = 0

    paid_on_iso = paid_on.isoformat() if paid_on else None

    for parsed in parse_file(path, tz):
        nid = _resolve_nanny(parsed.tags, tag_to_nanny_id)
        if nid is None:
            if not parsed.tags:
                skipped_empty += 1
            else:
                skipped_no_mapping += 1
            continue

        # Insert shift; ON CONFLICT(timetagger_key) DO NOTHING via try/except.
        try:
            cur = conn.execute(
                "INSERT INTO shifts ("
                " nanny_id, start_time, end_time, notes, source, confirmed,"
                " paid_on, timetagger_key, created_by, updated_by"
                ") VALUES (?, ?, ?, ?, 'imported', 1, ?, ?, ?, ?)",
                (
                    nid,
                    parsed.start_local.isoformat(),
                    parsed.end_local.isoformat(),
                    parsed.description or None,
                    paid_on_iso,
                    parsed.key,
                    created_by,
                    created_by,
                ),
            )
        except sqlite3.IntegrityError:
            skipped_already += 1
            continue

        shifts_inserted += 1
        shift_id = cur.lastrowid

        # Insert expense candidates (NOT marked paid — surface for review).
        for exp in parsed.expense_candidates:
            conn.execute(
                "INSERT INTO expenses (shift_id, amount_cents, description, pending_review)"
                " VALUES (?, ?, ?, 1)",
                (shift_id, exp.amount_cents, exp.description),
            )
            expenses_inserted += 1

    return ImportResult(
        shifts_inserted=shifts_inserted,
        expenses_inserted=expenses_inserted,
        rows_skipped_no_mapping=skipped_no_mapping,
        rows_skipped_already_imported=skipped_already,
        rows_skipped_empty=skipped_empty,
    )


def _resolve_nanny(tags: list[str], mapping: dict[str, int]) -> int | None:
    """Return nanny_id if exactly one mapped tag matches; else None.

    Multiple matching tags are treated as ambiguous (e.g. if both '#anita' and
    '#joy' tags somehow appeared on one row) and skipped.
    """
    matches = {mapping[t.lower()] for t in tags if t.lower() in mapping}
    if len(matches) == 1:
        return next(iter(matches))
    return None


def unique_tags(path: Path, tz: ZoneInfo) -> Counter:
    """Tally unique tags across the file. Useful for the interactive prompt."""
    counter: Counter = Counter()
    for parsed in parse_file(path, tz):
        for t in parsed.tags:
            counter[t.lower()] += 1
    return counter
