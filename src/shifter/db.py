from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(database_path: str | Path, *, migrate: bool = True) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit; we manage txns explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if migrate:
        # Idempotent — checks _migrations and only applies new files. Safe to call
        # on every connect; cheap when there's nothing to do.
        apply_migrations(conn)
    return conn


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations ("
        "  name TEXT PRIMARY KEY,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    applied = {row["name"] for row in conn.execute("SELECT name FROM _migrations")}
    newly_applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        # executescript manages its own transaction; we record the migration after.
        conn.executescript(path.read_text())
        conn.execute("INSERT INTO _migrations (name) VALUES (?)", (path.name,))
        newly_applied.append(path.name)
    return newly_applied


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
