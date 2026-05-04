from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from shifter.auth import current_user, require_api_key
from shifter.config import Settings, get_settings


def _client(settings: Settings) -> TestClient:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(user: str = Depends(current_user)) -> dict:
        return {"user": user}

    @app.post("/api/ping")
    def ping(_: None = Depends(require_api_key)) -> dict:
        return {"ok": True}

    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_current_user_missing_header_401():
    s = Settings(api_key="testtest", allowed_users="")
    r = _client(s).get("/whoami")
    assert r.status_code == 401


def test_current_user_allowed_when_no_allowlist():
    s = Settings(api_key="testtest", allowed_users="")
    r = _client(s).get("/whoami", headers={"Remote-User": "anyone"})
    assert r.status_code == 200
    assert r.json() == {"user": "anyone"}


def test_current_user_blocked_when_not_in_allowlist():
    s = Settings(api_key="testtest", allowed_users="alice,bob")
    r = _client(s).get("/whoami", headers={"Remote-User": "carol"})
    assert r.status_code == 403


def test_current_user_allowed_when_in_allowlist():
    s = Settings(api_key="testtest", allowed_users="alice,bob")
    r = _client(s).get("/whoami", headers={"Remote-User": "alice"})
    assert r.status_code == 200


def test_api_key_missing():
    s = Settings(api_key="topsecret", allowed_users="")
    r = _client(s).post("/api/ping")
    assert r.status_code == 401


def test_api_key_wrong():
    s = Settings(api_key="topsecret", allowed_users="")
    r = _client(s).post("/api/ping", headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_api_key_correct():
    s = Settings(api_key="topsecret", allowed_users="")
    r = _client(s).post("/api/ping", headers={"X-API-Key": "topsecret"})
    assert r.status_code == 200


def test_dev_mode_bypasses_remote_user():
    s = Settings(api_key="", allowed_users="alice", dev_mode=True, dev_user="dev")
    r = _client(s).get("/whoami")
    assert r.status_code == 200
    assert r.json() == {"user": "dev"}


def test_dev_mode_bypasses_api_key():
    s = Settings(api_key="", allowed_users="", dev_mode=True)
    r = _client(s).post("/api/ping")
    assert r.status_code == 200


def test_dev_mode_still_uses_provided_remote_user():
    s = Settings(api_key="", allowed_users="", dev_mode=True, dev_user="dev")
    r = _client(s).get("/whoami", headers={"Remote-User": "alice"})
    assert r.json() == {"user": "alice"}


def test_api_key_required_when_not_dev_mode():
    import pytest
    with pytest.raises(ValueError, match="API_KEY must be at least 8"):
        Settings(api_key="short", dev_mode=False)


def test_api_key_can_be_empty_in_dev_mode():
    # Should not raise.
    s = Settings(api_key="", dev_mode=True)
    assert s.dev_mode is True
