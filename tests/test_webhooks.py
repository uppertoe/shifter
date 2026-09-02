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
    assert "1 older shift awaiting review" in r.text
    assert 'href="/review"' in r.text


def test_review_page_lists_old_pending_with_snapshots_and_confirm(client, conn, tmp_path):
    """Shifts that aged off the dashboard must still be confirmable — with
    their snapshots — from /review."""
    old_day = date.today() - timedelta(days=40)
    sid = _make_shift_with_shots(conn, tmp_path / "shots", day=old_day)
    r = client.get("/review", headers={"Remote-User": "alice"})
    assert r.status_code == 200
    assert f"/shifts/{sid}/confirm" in r.text
    assert f"/screenshots/{old_day.year:04d}/{old_day.month:02d}/arr-{sid}.jpg" in r.text
    assert f"/screenshots/{old_day.year:04d}/{old_day.month:02d}/dep-{sid}.jpg" in r.text
    assert "data-lightbox" in r.text
    # Confirmed shifts don't show up.
    conn.execute("UPDATE shifts SET confirmed = 1 WHERE id = ?", (sid,))
    r = client.get("/review", headers={"Remote-User": "alice"})
    assert f"/shifts/{sid}/confirm" not in r.text
    assert "Nothing to review" in r.text


def test_confirm_from_review_page_skips_dashboard_oob(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/confirm",
                     headers={"HX-Request": "true", "Remote-User": "alice",
                              "HX-Current-URL": "http://testserver/review"})
    assert r.status_code == 200
    assert r.text == ""   # card vanishes, no dashboard fragments
    assert conn.execute("SELECT confirmed FROM shifts WHERE id = ?", (sid,)).fetchone()["confirmed"] == 1


def test_cancel_edit_from_review_page_restores_card(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.get(f"/shifts/{sid}/cancel-edit?row_id=review-shift-{sid}",
                    headers={"HX-Request": "true", "Remote-User": "alice",
                             "HX-Current-URL": "http://testserver/review"})
    assert r.status_code == 200
    assert f'id="review-shift-{sid}"' in r.text
    assert f"/shifts/{sid}/confirm" in r.text
    assert "dashboard-stats" not in r.text


def test_confirm_with_next_redirects_back(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/confirm",
                     data={"next": "/shifts?confirmed=no&nanny_id=1"},
                     headers={"Remote-User": "alice"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/shifts?confirmed=no&nanny_id=1"
    # Off-site "next" is refused.
    r = client.post(f"/shifts/{sid}/confirm", data={"next": "//evil.example/"},
                     headers={"Remote-User": "alice"}, follow_redirects=False)
    assert r.headers["location"] == "/shifts"


def test_shifts_list_pending_row_has_confirm(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-01T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.get("/shifts?confirmed=no", headers={"Remote-User": "alice"})
    assert f'action="/shifts/{sid}/confirm"' in r.text
    assert 'value="/shifts?confirmed=no"' in r.text


def test_create_shift_with_inline_expenses(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    r = client.post(
        "/shifts",
        data={"nanny_id": str(nid), "start_local": "2026-05-04T07:00",
              "end_local": "2026-05-04T17:00",
              "expense_description": ["lunch", "", "zoo"],
              "expense_amount": ["12.90", "", "$53"]},
        headers={"Remote-User": "alice"}, follow_redirects=False,
    )
    assert r.status_code == 303
    rows = conn.execute(
        "SELECT description, amount_cents FROM expenses ORDER BY id"
    ).fetchall()
    assert [(x["description"], x["amount_cents"]) for x in rows] == [("lunch", 1290), ("zoo", 5300)]


def test_create_shift_rejects_half_filled_expense_row(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    r = client.post(
        "/shifts",
        data={"nanny_id": str(nid), "start_local": "2026-05-04T07:00",
              "expense_description": "lunch", "expense_amount": ""},
        headers={"Remote-User": "alice"}, follow_redirects=False,
    )
    assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"] == 0


def test_edit_page_save_and_confirm(client, conn, tmp_path):
    sid = _make_shift_with_shots(conn, tmp_path / "shots", day=date(2026, 5, 4))
    r = client.get(f"/shifts/{sid}/edit", headers={"Remote-User": "alice"})
    assert "Save &amp; confirm" in r.text
    assert "/screenshots/2026/05/arr-" in r.text   # snapshots on the edit page too
    nid = conn.execute("SELECT nanny_id FROM shifts WHERE id = ?", (sid,)).fetchone()["nanny_id"]
    r = client.post(
        f"/shifts/{sid}",
        data={"nanny_id": str(nid), "start_local": "2026-05-04T07:45",
              "end_local": "2026-05-04T17:00", "confirm": "1"},
        headers={"Remote-User": "alice"}, follow_redirects=False,
    )
    assert r.status_code == 303
    row = conn.execute("SELECT confirmed, start_time FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert row["confirmed"] == 1
    assert row["start_time"].startswith("2026-05-04T07:45")


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


def test_pay_single_shift(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(
        f"/shifts/{sid}/pay",
        data={"paid_on": "2026-05-06", "paid_note": "cash"},
        headers={"Remote-User": "alice"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = conn.execute("SELECT paid_on, paid_note FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert row["paid_on"] == "2026-05-06"
    assert row["paid_note"] == "cash"


def test_pay_shift_includes_unpaid_expenses_when_checkbox_set(client, conn):
    """include_expenses=1 (the form default) settles the shift and any of its
    unpaid expenses in one go with the same paid_on date."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    eid_unpaid = conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description)"
        " VALUES (?, 500, 'lunch')", (sid,),
    ).lastrowid
    eid_already_paid = conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description, paid_on)"
        " VALUES (?, 200, 'parking', '2026-05-01')", (sid,),
    ).lastrowid
    client.post(
        f"/shifts/{sid}/pay",
        data={"paid_on": "2026-05-06", "include_expenses": "1"},
        headers={"Remote-User": "alice"},
    )
    rows = {
        r["id"]: r["paid_on"] for r in conn.execute(
            "SELECT id, paid_on FROM expenses WHERE shift_id = ?", (sid,)
        ).fetchall()
    }
    assert rows[eid_unpaid] == "2026-05-06"
    # Already-paid expenses must not have their paid_on date overwritten.
    assert rows[eid_already_paid] == "2026-05-01"


def test_pay_shift_does_not_touch_expenses_without_checkbox(client, conn):
    """Without include_expenses, only the shift is settled."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    eid = conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description)"
        " VALUES (?, 500, 'lunch')", (sid,),
    ).lastrowid
    client.post(
        f"/shifts/{sid}/pay",
        data={"paid_on": "2026-05-06"},  # no include_expenses key
        headers={"Remote-User": "alice"},
    )
    assert conn.execute(
        "SELECT paid_on FROM shifts WHERE id = ?", (sid,)
    ).fetchone()["paid_on"] == "2026-05-06"
    assert conn.execute(
        "SELECT paid_on FROM expenses WHERE id = ?", (eid,)
    ).fetchone()["paid_on"] is None


def test_pay_shift_redirects_to_next_when_safe(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(
        f"/shifts/{sid}/pay",
        data={"paid_on": "2026-05-06", "next": f"/nannies/{nid}/unpaid"},
        headers={"Remote-User": "alice"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/nannies/{nid}/unpaid"


def test_pay_shift_rejects_external_next(client, conn):
    """Unsafe ``next`` (protocol-relative or external) falls back to /shifts."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    for bad in ("//evil.com/path", "https://evil.com", "javascript:alert(1)", ""):
        r = client.post(
            f"/shifts/{sid}/pay",
            data={"paid_on": "2026-05-06", "next": bad},
            headers={"Remote-User": "alice"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/shifts"


def test_pay_shift_404_for_unknown_shift(client, conn):
    r = client.post(
        "/shifts/9999/pay",
        data={"paid_on": "2026-05-06"},
        headers={"Remote-User": "alice"},
    )
    assert r.status_code == 404


def test_pay_shift_400_for_bad_date(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(
        f"/shifts/{sid}/pay",
        data={"paid_on": "yesterday"},
        headers={"Remote-User": "alice"},
    )
    assert r.status_code == 400


def test_shifts_list_shows_pay_button_only_for_unpaid_closed(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    paid_sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, paid_on, source,"
        " confirmed, created_by, updated_by) VALUES (?,"
        " '2026-05-01T07:00:00+10:00', '2026-05-01T17:00:00+10:00',"
        " '2026-05-02', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    unpaid_sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?,"
        " '2026-05-04T07:00:00+10:00', '2026-05-04T17:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    open_sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?,"
        " '2026-05-05T07:00:00+10:00', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    body = client.get("/shifts", headers={"Remote-User": "alice"}).text
    assert f'action="/shifts/{unpaid_sid}/pay"' in body
    # Already-paid and still-open shifts shouldn't get the Paid action
    assert f'action="/shifts/{paid_sid}/pay"' not in body
    assert f'action="/shifts/{open_sid}/pay"' not in body


def test_pay_single_expense(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    conn.execute("INSERT INTO pay_rates (nanny_id, rate_cents, effective_from)"
                  " VALUES (?, 3500, '2025-01-01')", (nid,))
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    eid_keep = conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description)"
        " VALUES (?, 1450, 'Lunch')", (sid,),
    ).lastrowid
    eid_pay = conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description)"
        " VALUES (?, 800, 'Taxi')", (sid,),
    ).lastrowid
    r = client.post(
        f"/nannies/{nid}/expenses/{eid_pay}/pay",
        data={"paid_on": "2026-05-06"},
        headers={"Remote-User": "alice"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    rows = {e["id"]: e["paid_on"] for e in conn.execute(
        "SELECT id, paid_on FROM expenses WHERE shift_id = ?", (sid,)).fetchall()}
    assert rows[eid_pay] == "2026-05-06"
    assert rows[eid_keep] is None  # other expense untouched


def test_pay_single_expense_404_for_other_nanny(client, conn):
    """Can't pay an expense via the wrong nanny's URL."""
    n1 = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    n2 = conn.execute("INSERT INTO nannies (name) VALUES ('B')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (n1,),
    ).lastrowid
    eid = conn.execute(
        "INSERT INTO expenses (shift_id, amount_cents, description)"
        " VALUES (?, 500, 'snack')", (sid,),
    ).lastrowid
    r = client.post(
        f"/nannies/{n2}/expenses/{eid}/pay",
        data={"paid_on": "2026-05-06"},
        headers={"Remote-User": "alice"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_pay_all_expenses_for_nanny(client, conn):
    """Bulk pays all unpaid expenses; leaves shifts alone."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    for amount, desc in [(500, "snack"), (800, "taxi"), (1200, "lunch")]:
        conn.execute("INSERT INTO expenses (shift_id, amount_cents, description)"
                      " VALUES (?, ?, ?)", (sid, amount, desc))
    r = client.post(
        f"/nannies/{nid}/expenses/pay-all",
        data={"paid_on": "2026-05-06"},
        headers={"Remote-User": "alice"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    paid_count = conn.execute(
        "SELECT COUNT(*) FROM expenses WHERE shift_id = ? AND paid_on IS NOT NULL",
        (sid,),
    ).fetchone()[0]
    assert paid_count == 3
    # Shift NOT marked paid
    shift_paid = conn.execute("SELECT paid_on FROM shifts WHERE id = ?", (sid,)).fetchone()["paid_on"]
    assert shift_paid is None


def test_pay_all_expenses_handles_zero_unpaid(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    r = client.post(
        f"/nannies/{nid}/expenses/pay-all",
        data={"paid_on": "2026-05-06"},
        headers={"Remote-User": "alice"},
        follow_redirects=False,
    )
    assert r.status_code == 303  # no-op redirect, no error


def test_schedule_chip_click_cycle_for_manual_oneoff(client, conn):
    """Manual one-off: click 1 → cancelled, click 2 → deleted, no resurrection."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    eid = conn.execute(
        "INSERT INTO expected_shifts (nanny_id, date, start_time, end_time, source)"
        " VALUES (?, '2026-06-01', '07:00', '18:00', 'manual')", (nid,),
    ).lastrowid
    # Click 1: cancel
    r = client.post(
        f"/schedule/expected/{eid}/cancel",
        data={"nanny_id": str(nid), "on_date": "2026-06-01"},
        headers={"Remote-User": "alice"},
    )
    assert r.status_code == 200
    assert conn.execute(
        "SELECT cancelled FROM expected_shifts WHERE id = ?", (eid,)
    ).fetchone()["cancelled"] == 1
    # Click 2: delete
    r = client.post(
        f"/schedule/expected/{eid}/delete",
        data={"nanny_id": str(nid), "on_date": "2026-06-01"},
        headers={"Remote-User": "alice"},
    )
    assert r.status_code == 200
    assert conn.execute(
        "SELECT 1 FROM expected_shifts WHERE id = ?", (eid,)
    ).fetchone() is None


def test_schedule_chip_click_cycle_for_pattern_slot(client, conn):
    """Pattern slot: click 1 → cancelled, click 2 → re-materialized as active.
    The user-visible cycle is active → cancelled → active for recurring chips."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    today = date.today()
    pid = conn.execute(
        "INSERT INTO schedule_patterns "
        " (nanny_id, day_of_week, start_time, end_time, active_from)"
        " VALUES (?, ?, '07:00', '18:00', ?)",
        (nid, today.weekday(), today.isoformat()),
    ).lastrowid
    # Project the pattern to materialize this week's expected_shifts row.
    from shifter import schedule
    schedule.materialize(conn, from_date=today, to_date=today)
    eid = conn.execute(
        "SELECT id FROM expected_shifts WHERE pattern_id = ? AND date = ?",
        (pid, today.isoformat()),
    ).fetchone()["id"]
    # Click 1: cancel
    client.post(
        f"/schedule/expected/{eid}/cancel",
        data={"nanny_id": str(nid), "on_date": today.isoformat()},
        headers={"Remote-User": "alice"},
    )
    assert conn.execute(
        "SELECT cancelled FROM expected_shifts WHERE id = ?", (eid,)
    ).fetchone()["cancelled"] == 1
    # Click 2: delete — but the response handler re-materializes, so a
    # fresh active row appears at the same date/start_time. The original
    # id is gone; a new id replaces it.
    client.post(
        f"/schedule/expected/{eid}/delete",
        data={"nanny_id": str(nid), "on_date": today.isoformat()},
        headers={"Remote-User": "alice"},
    )
    rows = conn.execute(
        "SELECT id, cancelled FROM expected_shifts"
        " WHERE pattern_id = ? AND date = ?",
        (pid, today.isoformat()),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] != eid       # row was hard-deleted, then projected
    assert rows[0]["cancelled"] == 0  # back to active


def test_visibility_toggle_hides_nanny_from_dashboard(client, conn):
    """Hidden nannies (show_on_dashboard=0) should disappear from the
    dashboard's open-shift list, pending-review list, and per-nanny owed
    summaries — but stay listed under /nannies and /shifts."""
    visible = conn.execute(
        "INSERT INTO nannies (name) VALUES ('Visible')"
    ).lastrowid
    hidden = conn.execute(
        "INSERT INTO nannies (name) VALUES ('Hidden')"
    ).lastrowid
    for nid in (visible, hidden):
        conn.execute(
            "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
            " created_by, updated_by) VALUES (?, "
            "'2026-05-06T07:00:00+10:00', 'manual', 0, 'x', 'x')", (nid,),
        )
    # Hide the second nanny.
    r = client.post(
        f"/nannies/{hidden}/visibility",
        data={"show": "0"},
        headers={"Remote-User": "alice"},
    )
    assert r.status_code == 200
    flag = conn.execute(
        "SELECT show_on_dashboard FROM nannies WHERE id = ?", (hidden,)
    ).fetchone()["show_on_dashboard"]
    assert flag == 0

    # Dashboard should now reference Visible but not Hidden.
    body = client.get("/", headers={"Remote-User": "alice"}).text
    assert "Visible" in body
    assert "Hidden" not in body

    # /nannies still shows both.
    body = client.get("/nannies", headers={"Remote-User": "alice"}).text
    assert "Visible" in body
    assert "Hidden" in body

    # Re-show.
    client.post(
        f"/nannies/{hidden}/visibility",
        data={"show": "1"},
        headers={"Remote-User": "alice"},
    )
    body = client.get("/", headers={"Remote-User": "alice"}).text
    assert "Hidden" in body


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


def test_delete_with_htmx_returns_oob_refresh(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/delete",
                     headers={"HX-Request": "true", "Remote-User": "alice"})
    assert r.status_code == 200
    assert 'id="dashboard-owed"' in r.text
    assert 'hx-swap-oob="outerHTML"' in r.text
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
    assert 'hx-swap-oob="outerHTML"' in r.text
    row = conn.execute(
        "SELECT start_time, end_time, notes, confirmed FROM shifts WHERE id = ?", (sid,),
    ).fetchone()
    assert row["start_time"].startswith("2026-05-04T07:30:00")
    assert row["end_time"].startswith("2026-05-04T17:45:00")
    assert row["notes"] == "Adjusted by hand"
    assert row["confirmed"] == 1


def test_oob_refresh_includes_all_dashboard_sections(client, conn):
    """The OOB fragment that mutation endpoints append must cover every
    dashboard section that displays shift-derived data, so a delete or
    confirm keeps totals/counts in sync without a page reload."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    conn.execute(
        "INSERT INTO pay_rates (nanny_id, rate_cents, effective_from)"
        " VALUES (?, 3500, '2025-01-01')", (nid,),
    )
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'manual', 1, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/delete",
                     headers={"HX-Request": "true", "Remote-User": "alice"})
    assert r.status_code == 200
    body = r.text
    for section_id in ("dashboard-stats", "dashboard-open", "dashboard-pending",
                         "dashboard-unresolved", "dashboard-owed"):
        assert f'id="{section_id}"' in body, f"missing OOB fragment for {section_id}"


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
    # Response carries OOB-swap fragments so dashboard totals re-render.
    assert 'hx-swap-oob="outerHTML"' in r.text
    assert 'id="dashboard-owed"' in r.text
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


def test_confirm_with_htmx_returns_oob_refresh(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.post(f"/shifts/{sid}/confirm",
                     headers={"HX-Request": "true", "Remote-User": "alice"})
    assert r.status_code == 200
    assert 'id="dashboard-owed"' in r.text
    assert 'id="dashboard-stats"' in r.text
    row = conn.execute("SELECT confirmed FROM shifts WHERE id = ?", (sid,)).fetchone()
    assert row["confirmed"] == 1


def test_confirm_batch_confirms_all_passed_ids(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    ids = []
    for d in ("2026-05-04", "2026-05-05", "2026-05-06"):
        sid = conn.execute(
            "INSERT INTO shifts (nanny_id, start_time, end_time, source,"
            " confirmed, created_by, updated_by)"
            " VALUES (?, ?, ?, 'ha', 0, 'x', 'x')",
            (nid, f"{d}T07:00:00+10:00", f"{d}T18:00:00+10:00"),
        ).lastrowid
        ids.append(sid)
    r = client.post(
        "/shifts/confirm-batch",
        data={"shift_ids": [str(i) for i in ids]},
        headers={"HX-Request": "true", "Remote-User": "alice"},
    )
    assert r.status_code == 200, r.text
    rows = conn.execute(
        f"SELECT confirmed FROM shifts WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    assert all(row["confirmed"] == 1 for row in rows)


def test_confirm_batch_does_not_touch_unlisted_shifts(client, conn):
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    listed = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source,"
        " confirmed, created_by, updated_by) VALUES (?, "
        "'2026-05-04T07:00:00+10:00', '2026-05-04T18:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    untouched = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source,"
        " confirmed, created_by, updated_by) VALUES (?, "
        "'2026-04-01T07:00:00+10:00', '2026-04-01T18:00:00+10:00',"
        " 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    client.post(
        "/shifts/confirm-batch",
        data={"shift_ids": [str(listed)]},
        headers={"HX-Request": "true", "Remote-User": "alice"},
    )
    assert conn.execute(
        "SELECT confirmed FROM shifts WHERE id = ?", (listed,)
    ).fetchone()["confirmed"] == 1
    assert conn.execute(
        "SELECT confirmed FROM shifts WHERE id = ?", (untouched,)
    ).fetchone()["confirmed"] == 0


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


def test_edit_page_blank_notes_not_rendered_as_none(client, conn):
    """Regression: a NULL notes column rendered the literal string 'None' in the
    textarea, which would then be saved back as the note on the next Save."""
    nid = conn.execute("INSERT INTO nannies (name) VALUES ('A')").lastrowid
    sid = conn.execute(
        "INSERT INTO shifts (nanny_id, start_time, end_time, source, confirmed,"
        " created_by, updated_by) VALUES (?, '2026-05-04T07:00:00+10:00',"
        " '2026-05-04T17:00:00+10:00', 'ha', 0, 'x', 'x')", (nid,),
    ).lastrowid
    r = client.get(f"/shifts/{sid}/edit", headers={"Remote-User": "alice"})
    assert r.status_code == 200
    assert 'placeholder="optional"></textarea>' in r.text
    assert ">None</textarea>" not in r.text
