from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from shifter import importer

MEL = ZoneInfo("Australia/Melbourne")


@pytest.fixture
def fixture_path() -> Path:
    return Path(__file__).parent / "fixtures" / "timetagger_sample.tsv"


def _seed_nannies(conn) -> tuple[int, int]:
    a = conn.execute("INSERT INTO nannies (name) VALUES ('Anita')").lastrowid
    j = conn.execute("INSERT INTO nannies (name) VALUES ('Joy')").lastrowid
    return a, j


# --- expense extraction -----------------------------------------------------

@pytest.mark.parametrize("desc, expected", [
    ("Add $12 for lunch", [("lunch", 1200)]),
    ("lunch $10", [("lunch", 1000)]),
    ("+ lunch $7.80 sushi", [("lunch", 780)]),
    ("lunch $15.90", [("lunch", 1590)]),
    ("lunch $42.41", [("lunch", 4241)]),
    ("+ $53 for aquarium", [("aquarium", 5300)]),
    ("lunch 46.50", [("lunch", 4650)]),
    ("+ lunch $14", [("lunch", 1400)]),
    ("lunch 46.49", [("lunch", 4649)]),
    ("#joy #nanny - lunch $10", [("lunch", 1000)]),
    ("plain notes, nothing here", []),
    ("overnight? rate", []),
    ("", []),
])
def test_extract_expenses(desc, expected):
    out = importer.extract_expenses(desc)
    assert [(e.description, e.amount_cents) for e in out] == expected


def test_extract_expenses_dedupes_by_amount():
    # Same amount appearing twice should only be counted once.
    out = importer.extract_expenses("lunch $10 and tip $10")
    assert len(out) == 1


def test_parse_amount_to_cents():
    assert importer.parse_amount_to_cents("38") == 3800
    assert importer.parse_amount_to_cents("38.00") == 3800
    assert importer.parse_amount_to_cents("38.50") == 3850
    assert importer.parse_amount_to_cents("38.5") == 3850
    assert importer.parse_amount_to_cents("0.99") == 99


# --- file parsing -----------------------------------------------------------

def test_parse_timestamp_iso_z(tmp_path):
    """ISO 8601 with Z (UTC) is converted to the configured local TZ."""
    dt = importer._parse_timestamp("2025-04-01T07:00:00Z", MEL)
    assert dt is not None and dt.tzinfo is MEL
    # 07:00 UTC on 2025-04-01 = 18:00 local AEDT (+11)
    assert dt.hour == 18
    assert dt.day == 1


def test_parse_timestamp_iso_with_offset(tmp_path):
    """Numeric offset times are converted to the configured TZ. April 1 in
    Melbourne is AEDT (+11), so a +10 input shifts forward by an hour."""
    dt = importer._parse_timestamp("2025-04-01T07:00:00+10:00", MEL)
    assert dt is not None and dt.hour == 8 and dt.minute == 0


def test_parse_timestamp_legacy_naive(tmp_path):
    dt = importer._parse_timestamp("2025-04-01 07:00:00", MEL)
    assert dt is not None and dt.tzinfo is MEL and dt.hour == 7


def test_parse_timestamp_garbage_returns_none():
    assert importer._parse_timestamp("not a date", MEL) is None


def test_parse_file_accepts_iso8601(tmp_path):
    """Regression: a TimeTagger export with ISO 8601 timestamps used to
    silently skip every row because the parser only accepted the legacy
    naive format."""
    p = tmp_path / "iso.tsv"
    p.write_text(
        "key\tstart\tstop\ttags\tdescription\n"
        "abc\t2025-04-01T07:00:00Z\t2025-04-01T18:00:00Z\t#anita\tday shift\n"
    )
    rows = list(importer.parse_file(p, MEL))
    assert len(rows) == 1
    assert rows[0].key == "abc"
    # 07:00 UTC = 18:00 AEDT
    assert rows[0].start_local.hour == 18


def test_parse_file_loads_rows(fixture_path):
    rows = list(importer.parse_file(fixture_path, MEL))
    # 13 data rows: 10 nanny shifts + 2 oncall + 1 empty-tag row
    assert len(rows) == 13
    # First row is Anita 1 Apr
    r = rows[0]
    assert r.key == "jycKazWf"
    assert r.start_local.isoformat() == "2025-04-01T07:00:00+11:00"  # AEDT
    assert "anita" in r.tags
    assert "nanny" in r.tags


def test_parse_file_handles_midnight_crossing(fixture_path):
    rows = [r for r in importer.parse_file(fixture_path, MEL) if r.key == "HachoNcB"]
    r = rows[0]
    assert r.start_local.date().isoformat() == "2025-09-12"
    assert r.end_local.date().isoformat() == "2025-09-13"


def test_parse_file_marks_not_worked(fixture_path):
    rows = {r.key: r for r in importer.parse_file(fixture_path, MEL)}
    assert rows["qGQXycWq"].not_worked_note is True
    assert rows["khKeAAdr"].not_worked_note is True
    assert rows["jycKazWf"].not_worked_note is False


# --- planning ---------------------------------------------------------------

def test_plan_skips_unmapped_and_empty(conn, fixture_path):
    a, j = _seed_nannies(conn)
    plan = importer.plan_import(conn, fixture_path, {"anita": a, "joy": j}, tz=MEL)
    # 4 anita + 6 joy = 10 nanny rows. 2 oncall = unmapped. 1 empty-tag = skipped_empty.
    assert plan.by_nanny_id == {a: 4, j: 6}
    assert plan.rows_skipped_no_mapping == 2
    assert plan.rows_skipped_empty == 1
    assert plan.expense_candidates_total >= 6  # at least the obvious lunches/aquarium
    assert plan.not_worked_count == 2
    # Most common unmapped tag should be 'oncall' or similar
    assert plan.skipped_unique_tags.most_common(1)[0][0] in {"alex", "oncall", "worksite"}


# --- import -----------------------------------------------------------------

def test_run_import_inserts_shifts_and_expenses(conn, fixture_path):
    a, j = _seed_nannies(conn)
    paid = date(2026, 5, 4)
    result = importer.run_import(
        conn, fixture_path, {"anita": a, "joy": j}, tz=MEL, paid_on=paid,
    )
    assert result.shifts_inserted == 10
    # 4+ extracted lunches + aquarium etc.
    assert result.expenses_inserted >= 6

    # All inserted shifts should be marked paid on the supplied date
    rows = conn.execute("SELECT paid_on FROM shifts").fetchall()
    assert all(r["paid_on"] == "2026-05-04" for r in rows)

    # Source = imported, confirmed = 1
    rows = conn.execute("SELECT source, confirmed FROM shifts").fetchall()
    assert all(r["source"] == "imported" and r["confirmed"] == 1 for r in rows)

    # Expenses are pending_review and unpaid
    exp = conn.execute("SELECT pending_review, paid_on FROM expenses").fetchall()
    assert all(e["pending_review"] == 1 and e["paid_on"] is None for e in exp)


def test_run_import_is_idempotent(conn, fixture_path):
    a, j = _seed_nannies(conn)
    paid = date(2026, 5, 4)
    r1 = importer.run_import(conn, fixture_path, {"anita": a, "joy": j}, tz=MEL, paid_on=paid)
    r2 = importer.run_import(conn, fixture_path, {"anita": a, "joy": j}, tz=MEL, paid_on=paid)
    assert r2.shifts_inserted == 0
    assert r2.rows_skipped_already_imported == r1.shifts_inserted


def test_run_import_unpaid_when_no_paid_on(conn, fixture_path):
    a, j = _seed_nannies(conn)
    importer.run_import(conn, fixture_path, {"anita": a, "joy": j}, tz=MEL, paid_on=None)
    rows = conn.execute("SELECT paid_on FROM shifts").fetchall()
    assert all(r["paid_on"] is None for r in rows)


def test_run_import_filters_oncall_rows(conn, fixture_path):
    a, j = _seed_nannies(conn)
    importer.run_import(conn, fixture_path, {"anita": a, "joy": j}, tz=MEL, paid_on=None)
    keys = {r["timetagger_key"] for r in conn.execute("SELECT timetagger_key FROM shifts")}
    # The oncall rows should NOT be present
    assert "zegAoJvJ" not in keys
    assert "HachoNcB" not in keys
    # The empty-tag row should NOT be present
    assert "aPPGKPmt" not in keys
    # Nanny rows should be present
    assert "jycKazWf" in keys
    assert "GlnSJSeO" in keys


def test_unique_tags(fixture_path):
    counter = importer.unique_tags(fixture_path, MEL)
    assert counter["anita"] >= 1
    assert counter["joy"] >= 1
    assert counter["oncall"] >= 1
    assert counter["nanny"] >= 1
