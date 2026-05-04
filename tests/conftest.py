from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shifter.db import apply_migrations


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture
def sample_csv_path() -> Path:
    return Path(__file__).parent / "fixtures" / "timetagger_sample.tsv"
