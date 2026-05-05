from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from shifter import repos
from shifter.config import Settings, get_settings
from shifter.main import app, get_db

MEL = ZoneInfo("Australia/Melbourne")


@pytest.fixture
def client(conn, monkeypatch, tmp_path):
    """A TestClient backed by the in-memory `conn` fixture, with the lifespan
    short-circuited so it doesn't try to open a real DB file or start the
    cleanup loop."""
    settings = Settings(api_key="topsecret", allowed_users="",
                         database_path=tmp_path / "unused.db",
                         screenshot_dir=tmp_path / "shots")

    def _fake_get_settings():
        return settings

    monkeypatch.setattr("shifter.main.get_settings", _fake_get_settings)
    monkeypatch.setattr("shifter.main.cleanup_loop",
                         lambda *a, **kw: _noop())  # avoids touching disk

    app.dependency_overrides[get_db] = lambda: conn
    app.dependency_overrides[get_settings] = _fake_get_settings
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


async def _noop():
    return None


def _seed(conn, *, name="Jane"):
    return conn.execute("INSERT INTO nannies (name) VALUES (?)", (name,)).lastrowid


# --- auth -------------------------------------------------------------------

def test_current_shift_requires_api_key(client):
    r = client.get("/api/shift/current")
    assert r.status_code == 401


def test_current_shift_wrong_key(client):
    r = client.get("/api/shift/current", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


# --- payload shape ----------------------------------------------------------

def test_current_shift_empty_state(client):
    r = client.get("/api/shift/current", headers={"X-API-Key": "topsecret"})
    assert r.status_code == 200
    assert r.json() == {"shift": None, "unresolved_count": 0, "last_event": None}


def test_current_shift_reports_open_shift(client, conn):
    nid = _seed(conn)
    started = datetime.now(tz=MEL) - timedelta(minutes=42)
    repos.create_shift(
        conn, nanny_id=nid, start_time=started.isoformat(),
        end_time=None, rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="manual", confirmed=True, created_by="t",
    )
    body = client.get("/api/shift/current",
                       headers={"X-API-Key": "topsecret"}).json()
    assert body["shift"]["nanny_name"] == "Jane"
    assert 41 <= body["shift"]["duration_minutes"] <= 43
    assert body["shift"]["started_at"].startswith(started.isoformat()[:16])


def test_current_shift_picks_most_recent_open(client, conn):
    a = _seed(conn, name="Anita")
    j = _seed(conn, name="Joy")
    older = (datetime.now(tz=MEL) - timedelta(hours=4)).isoformat()
    newer = (datetime.now(tz=MEL) - timedelta(minutes=10)).isoformat()
    for nid, st in [(a, older), (j, newer)]:
        repos.create_shift(
            conn, nanny_id=nid, start_time=st, end_time=None,
            rate_override_cents=None, flat_rate_cents=None,
            notes=None, source="manual", confirmed=True, created_by="t",
        )
    body = client.get("/api/shift/current",
                       headers={"X-API-Key": "topsecret"}).json()
    assert body["shift"]["nanny_name"] == "Joy"


def test_current_shift_ignores_closed_shifts(client, conn):
    nid = _seed(conn)
    repos.create_shift(
        conn, nanny_id=nid,
        start_time=(datetime.now(tz=MEL) - timedelta(hours=8)).isoformat(),
        end_time=(datetime.now(tz=MEL) - timedelta(hours=1)).isoformat(),
        rate_override_cents=None, flat_rate_cents=None,
        notes=None, source="manual", confirmed=True, created_by="t",
    )
    body = client.get("/api/shift/current",
                       headers={"X-API-Key": "topsecret"}).json()
    assert body["shift"] is None


def test_current_shift_counts_unresolved_and_returns_last_event(client, conn):
    conn.execute(
        "INSERT INTO ha_events (occurred_at, source, resolution)"
        " VALUES (?, 'a', 'unresolved')",
        ("2026-05-04T07:00:00+10:00",),
    )
    conn.execute(
        "INSERT INTO ha_events (occurred_at, source, resolution)"
        " VALUES (?, 'b', 'unresolved')",
        ("2026-05-04T07:30:00+10:00",),
    )
    conn.execute(
        "INSERT INTO ha_events (occurred_at, source, resolution)"
        " VALUES (?, 'c', 'arrival')",
        ("2026-05-04T09:00:00+10:00",),
    )
    body = client.get("/api/shift/current",
                       headers={"X-API-Key": "topsecret"}).json()
    assert body["unresolved_count"] == 2
    assert body["last_event"]["resolution"] == "arrival"
    assert body["last_event"]["occurred_at"] == "2026-05-04T09:00:00+10:00"
