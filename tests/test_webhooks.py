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
    shots_dir = tmp_path / "shots"
    shots_dir.mkdir()
    settings = Settings(api_key="topsecret", allowed_users="",
                         database_path=tmp_path / "unused.db",
                         screenshot_dir=shots_dir,
                         frigate_base_url="https://frigate.test")

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


# --- regression: empty nanny_id from "All nannies" dropdown -----------------

def test_shifts_list_accepts_empty_nanny_id(client):
    """The filter form's <option value=""> for "All nannies" submits
    nanny_id= (empty). Must not 422."""
    r = client.get("/shifts?nanny_id=&open_only=1",
                    headers={"Remote-User": "alice"})
    assert r.status_code == 200, r.text


def test_reports_accepts_empty_nanny_id(client):
    r = client.get("/reports?nanny_id=&preset=this_fy",
                    headers={"Remote-User": "alice"})
    assert r.status_code == 200, r.text


# --- screenshot serving + dashboard pending-review surface ------------------

_helper_seq = [0]


def _make_shift_with_shots(conn, shots_dir, *, day, has_arrival=True, has_departure=True):
    """Fixture helper: create a pending HA shift with optional snapshots on disk."""
    _helper_seq[0] += 1
    nid = conn.execute(
        "INSERT INTO nannies (name) VALUES (?)", (f"Nanny{_helper_seq[0]}",)
    ).lastrowid
    start = datetime(day.year, day.month, day.day, 7, 30, tzinfo=MEL)
    end = datetime(day.year, day.month, day.day, 17, 0, tzinfo=MEL)
    shift_id = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, ?, ?, 'ha', 0, 'ha-webhook', 'ha-webhook')",
        (nid, start.isoformat(), end.isoformat()),
    ).lastrowid
    if has_arrival:
        eid = conn.execute(
            "INSERT INTO ha_events (occurred_at, source, nanny_id, shift_id, resolution)"
            " VALUES (?, 'cam', ?, ?, 'arrival')",
            (start.isoformat(), nid, shift_id),
        ).lastrowid
        rel = f"{day.year:04d}/{day.month:02d}/arr-{shift_id}.jpg"
        (shots_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (shots_dir / rel).write_bytes(b"\xff\xd8arrival")
        conn.execute(
            "INSERT INTO screenshots (ha_event_id, filename, content_type, size_bytes)"
            " VALUES (?, ?, 'image/jpeg', 7)", (eid, rel),
        )
    if has_departure:
        eid = conn.execute(
            "INSERT INTO ha_events (occurred_at, source, nanny_id, shift_id, resolution)"
            " VALUES (?, 'cam', ?, ?, 'departure')",
            (end.isoformat(), nid, shift_id),
        ).lastrowid
        rel = f"{day.year:04d}/{day.month:02d}/dep-{shift_id}.jpg"
        (shots_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (shots_dir / rel).write_bytes(b"\xff\xd8departure")
        conn.execute(
            "INSERT INTO screenshots (ha_event_id, filename, content_type, size_bytes)"
            " VALUES (?, ?, 'image/jpeg', 9)", (eid, rel),
        )
    return shift_id


def test_screenshot_serving_requires_login(client, conn, tmp_path):
    shots_dir = tmp_path / "shots"
    rel = "2026/05/test.jpg"
    (shots_dir / "2026/05").mkdir(parents=True, exist_ok=True)
    (shots_dir / rel).write_bytes(b"\xff\xd8x")
    # No Remote-User header → 401
    assert client.get(f"/screenshots/{rel}").status_code == 401


def test_screenshot_serving_returns_file(client, tmp_path):
    shots_dir = tmp_path / "shots"
    rel = "2026/05/served.jpg"
    (shots_dir / "2026/05").mkdir(parents=True, exist_ok=True)
    (shots_dir / rel).write_bytes(b"\xff\xd8payload")
    r = client.get(f"/screenshots/{rel}", headers={"Remote-User": "alice"})
    assert r.status_code == 200
    assert r.content == b"\xff\xd8payload"


def test_screenshot_serving_blocks_traversal(client):
    r = client.get("/screenshots/../../../etc/passwd",
                    headers={"Remote-User": "alice"})
    assert r.status_code == 404


def test_dashboard_renders_pending_with_thumbnails(client, conn, tmp_path):
    today = date.today()
    sid = _make_shift_with_shots(conn, tmp_path / "shots", day=today)
    r = client.get("/", headers={"Remote-User": "alice"})
    assert r.status_code == 200
    body = r.text
    assert "Pending review" in body
    assert "/screenshots/" in body  # a thumbnail was rendered
    assert "https://frigate.test/review?date=" in body
    assert f"/shifts/{sid}/confirm" in body


def test_dashboard_groups_older_pending_into_count(client, conn, tmp_path):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    # one this week, one from before
    _make_shift_with_shots(conn, tmp_path / "shots", day=today, has_departure=False)
    older_day = week_start - timedelta(days=3)
    _make_shift_with_shots(conn, tmp_path / "shots", day=older_day,
                            has_arrival=False, has_departure=False)
    r = client.get("/", headers={"Remote-User": "alice"})
    assert r.status_code == 200
    assert "1 older awaiting review" in r.text
    assert "/shifts?confirmed=no" in r.text


def test_shifts_list_filter_confirmed_no(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    pending_id = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-01T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    confirmed_id = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-02T07:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.get("/shifts?confirmed=no", headers={"Remote-User": "alice"})
    assert r.status_code == 200
    # Each row links to /shifts/<id>/edit; check by id, not by date string.
    assert f"/shifts/{pending_id}/edit" in r.text
    assert f"/shifts/{confirmed_id}/edit" not in r.text


def test_delete_shift_actually_deletes(client, conn):
    """Regression: the Delete form on the edit page used to be nested inside
    the Save form, which is invalid HTML — browsers silently submitted to
    the outer form's action and the delete never ran."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description)"
        " VALUES (?, 100, 'lunch')", (sid,),
    )
    r = client.post(f"/shifts/{sid}/delete",
                     headers={"Remote-User": "alice"},
                     follow_redirects=False)
    assert r.status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE id = ?", (sid,)).fetchone()[0] == 0
    # cascade: expenses gone too
    assert conn.execute("SELECT COUNT(*) FROM expenses WHERE shift_id = ?", (sid,)).fetchone()[0] == 0


def test_delete_with_htmx_returns_empty_body(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/delete",
                     headers={"HX-Request": "true", "Remote-User": "alice"})
    assert r.status_code == 200
    assert r.text == ""
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE id = ?", (sid,)).fetchone()[0] == 0


def test_inline_editor_includes_delete_button(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    body = client.get(
        f"/shifts/{sid}/inline-editor?row_id=open-shift-{sid}",
        headers={"Remote-User": "alice"},
    ).text
    # Delete form is OUTSIDE the save form (after the first </form>).
    save_close = body.find("</form>")
    delete_post = body.find(f'hx-post="/shifts/{sid}/delete"')
    assert save_close != -1 and delete_post != -1
    assert delete_post > save_close, "delete form must not be nested in save form"


def test_edit_page_delete_form_is_not_nested(client, conn):
    """The Delete form must live outside the outer Save form."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    body = client.get(f"/shifts/{sid}/edit",
                       headers={"Remote-User": "alice"}).text
    # The delete form's action must appear AFTER the outer form's </form>.
    save_close = body.find("</form>")
    delete_action = body.find(f'action="/shifts/{sid}/delete"')
    assert save_close != -1 and delete_action != -1
    assert delete_action > save_close, "delete form must not be nested in save form"


def test_inline_editor_renders_with_expenses(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T18:00:00+10:00', 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description)"
        " VALUES (?, 1450, 'Lunch')", (sid,),
    )
    r = client.get(
        f"/shifts/{sid}/inline-editor?row_id=pending-shift-{sid}&confirm_on_save=1",
        headers={"Remote-User": "alice"},
    )
    assert r.status_code == 200
    body = r.text
    assert f'id="pending-shift-{sid}"' in body
    assert "inline-editing" in body
    assert "Save &amp; confirm" in body
    assert "Lunch" in body  # existing expense rendered
    assert f'id="expenses-block-{sid}"' in body  # per-shift target


def test_inline_save_with_confirm(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T18:00:00+10:00', 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(
        f"/shifts/{sid}/inline-save",
        data={
            "start_local": "2026-05-04T07:30",
            "end_local": "2026-05-04T17:45",
            "notes": "Adjusted by hand",
            "confirm": "1",
        },
        headers={"HX-Request": "true", "Remote-User": "alice"},
    )
    assert r.status_code == 200
    assert r.text == ""
    row = conn.execute(
        "SELECT start_time, end_time, notes, confirmed FROM shifts WHERE id = ?", (sid,),
    ).fetchone()
    assert row["start_time"].startswith("2026-05-04T07:30:00")
    assert row["end_time"].startswith("2026-05-04T17:45:00")
    assert row["notes"] == "Adjusted by hand"
    assert row["confirmed"] == 1


def test_inline_save_without_confirm_keeps_unconfirmed(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(
        f"/shifts/{sid}/inline-save",
        data={"start_local": "2026-05-04T07:15", "end_local": "", "notes": "",
              "confirm": "0"},
        headers={"HX-Request": "true", "Remote-User": "alice"},
    )
    assert r.status_code == 200
    row = conn.execute("SELECT confirmed, end_time FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert row["confirmed"] == 0
    assert row["end_time"] is None


def test_inline_save_rejects_end_before_start(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(
        f"/shifts/{sid}/inline-save",
        data={"start_local": "2026-05-04T10:00", "end_local": "2026-05-04T09:00",
              "notes": "", "confirm": "0"},
        headers={"Remote-User": "alice"},
    )
    assert r.status_code == 400


def test_close_with_explicit_end_time(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/close",
                     data={"end_time": "2026-05-04T17:30"},
                     headers={"HX-Request": "true", "Remote-User": "alice"})
    assert r.status_code == 200
    assert r.text == ""
    end = conn.execute("SELECT end_time FROM shifts WHERE id = ?", (sid,)).fetchone()["end_time"]
    assert end.startswith("2026-05-04T17:30:00")


def test_close_rejects_end_before_start(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T10:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/close",
                     data={"end_time": "2026-05-04T09:00"},
                     headers={"Remote-User": "alice"})
    assert r.status_code == 400
    end = conn.execute("SELECT end_time FROM shifts WHERE id = ?", (sid,)).fetchone()["end_time"]
    assert end is None


def test_close_without_end_time_uses_now(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    # Start the shift in the recent past so any 'now' is a valid end time.
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2020-01-01T00:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/close",
                     headers={"HX-Request": "true", "Remote-User": "alice"})
    assert r.status_code == 200
    end = conn.execute("SELECT end_time FROM shifts WHERE id = ?", (sid,)).fetchone()["end_time"]
    assert end is not None


def test_confirm_with_htmx_returns_empty_body(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/confirm",
                     headers={"HX-Request": "true", "Remote-User": "alice"})
    assert r.status_code == 200
    assert r.text == ""
    row = conn.execute("SELECT confirmed FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert row["confirmed"] == 1


def test_confirm_without_htmx_redirects(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/confirm",
                     headers={"Remote-User": "alice"},
                     follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/shifts"


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
