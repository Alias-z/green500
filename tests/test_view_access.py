"""Verify isolated password sessions for the read-only Green500 view."""

import importlib
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from green500 import view_access, web


@pytest.fixture(autouse=True)
def clear_failed_logins():
    """Keep the per-process login allowance independent between tests."""
    view_access._FAILED_LOGINS.clear()
    yield
    view_access._FAILED_LOGINS.clear()


@pytest.fixture
def viewer_app(monkeypatch, tmp_path):
    """Create viewer and admin routes with different configured credentials."""
    settings = SimpleNamespace(
        view_password="viewer-secret", ops_token="admin-secret", data_dir=tmp_path
    )
    monkeypatch.setattr(view_access, "load_settings", lambda: settings)
    monkeypatch.setattr(web, "load_settings", lambda: settings)
    app = FastAPI()
    app.include_router(view_access.router)

    @app.get("/private", dependencies=[Depends(view_access.authorize_view)])
    def private():
        return {"visible": True}

    @app.post("/admin", dependencies=[Depends(web.authorize)])
    def admin():
        return {"admin": True}

    return app


def login(client, password="viewer-secret", origin="https://green.example"):
    """Make one browser-shaped viewer login request."""
    return client.post(
        "/api/login", json={"password": password}, headers={"Origin": origin}
    )


def test_password_failure_and_failed_login_throttle(viewer_app):
    """Wrong passwords create no cookie and repeated failures receive a short throttle."""
    client = TestClient(viewer_app, base_url="https://green.example")
    for _ in range(view_access.FAILED_LOGIN_LIMIT):
        result = login(client, "wrong")
        assert result.status_code == 401
        assert view_access.COOKIE_NAME not in client.cookies
    assert login(client, "wrong").status_code == 429


def test_unicode_password_and_malformed_cookies_fail_without_server_errors(viewer_app):
    """Arbitrary Unicode and invalid token shapes are ordinary authentication failures."""
    client = TestClient(viewer_app, base_url="https://green.example")
    assert login(client, "pässword-密碼").status_code == 401

    unicode_tokens = [
        "٢٠٠٠٠٠٠٠٠٠." + "a" * 64,
        "2000000000." + "é" * 64,
    ]
    assert all(not view_access._valid_session(token) for token in unicode_tokens)

    malformed = [
        "2000000000." + "a" * 63,
        "2000000000." + "a" * 500,
        "2000000000.extra." + "a" * 64,
    ]
    for token in malformed:
        client.cookies.set(view_access.COOKIE_NAME, token)
        assert client.get("/api/session").json() == {"authenticated": False}
        assert client.get("/private").status_code == 401


def test_failed_login_clients_are_bounded_and_expire(monkeypatch):
    """Unique failed-login identifiers cannot grow the process map without limit."""
    monkeypatch.setattr(view_access, "FAILED_LOGIN_CLIENT_LIMIT", 3)
    for index in range(4):
        view_access._record_failed_login(f"client-{index}", 100.0 + index)
    assert len(view_access._FAILED_LOGINS) == 3
    assert "client-0" not in view_access._FAILED_LOGINS

    assert view_access._is_throttled("new-client", 200.0) is False
    assert view_access._FAILED_LOGINS == {}


def test_cookie_authentication_tampering_and_expiry(viewer_app, monkeypatch):
    """Only an unmodified signed cookie before its absolute expiry authorizes the view."""
    now = 2_000_000_000.0
    monkeypatch.setattr(view_access.time, "time", lambda: now)
    client = TestClient(viewer_app, base_url="https://green.example")

    assert client.get("/private").status_code == 401
    result = login(client)
    assert result.status_code == 200
    token = client.cookies[view_access.COOKIE_NAME]
    assert "viewer-secret" not in token and "admin-secret" not in token
    assert client.get("/api/session").json() == {"authenticated": True}
    assert client.get("/private").json() == {"visible": True}

    client.cookies.set(
        view_access.COOKIE_NAME, token[:-1] + ("0" if token[-1] != "0" else "1")
    )
    assert client.get("/api/session").json() == {"authenticated": False}
    assert client.get("/private").status_code == 401

    client.cookies.set(view_access.COOKIE_NAME, token)
    monkeypatch.setattr(
        view_access.time,
        "time",
        lambda: now + view_access.SESSION_SECONDS + 1,
    )
    assert client.get("/api/session").json() == {"authenticated": False}
    assert client.get("/private").status_code == 401


def test_logout_expires_cookie_and_cross_origin_writes_fail(viewer_app):
    """Logout clears the session, and another website cannot log in or out a browser."""
    client = TestClient(viewer_app, base_url="https://green.example")
    assert login(client, origin="https://unrelated.example").status_code == 403
    assert login(client).status_code == 200
    assert (
        client.post(
            "/api/logout", headers={"Origin": "https://unrelated.example"}
        ).status_code
        == 403
    )
    result = client.post("/api/logout", headers={"Origin": "https://green.example"})
    assert result.json() == {"authenticated": False}
    assert "Max-Age=0" in result.headers["set-cookie"]
    assert client.get("/api/session").json() == {"authenticated": False}


def test_https_and_forwarded_https_set_secure_cookie(viewer_app):
    """Direct and Cloudflare-forwarded HTTPS sessions receive Secure cookies."""
    direct_client = TestClient(viewer_app, base_url="https://green.example")
    direct_cookie = login(direct_client).headers["set-cookie"].lower()
    assert "httponly" in direct_cookie
    assert "samesite=lax" in direct_cookie
    assert "max-age=2592000" in direct_cookie
    assert "secure" in direct_cookie

    forwarded_client = TestClient(viewer_app, base_url="http://127.0.0.1:19973")
    result = forwarded_client.post(
        "/api/login",
        json={"password": "viewer-secret"},
        headers={
            "Host": "green.example",
            "Origin": "https://green.example",
            "X-Forwarded-Proto": "https",
        },
    )
    assert result.status_code == 200
    assert "secure" in result.headers["set-cookie"].lower()


def test_session_rolls_forward_for_another_thirty_days(viewer_app, monkeypatch):
    """A valid session check renews both the signed expiry and browser lifetime."""
    now = 2_000_000_000.0
    monkeypatch.setattr(view_access.time, "time", lambda: now)
    client = TestClient(viewer_app, base_url="https://green.example")
    assert login(client).status_code == 200
    original_token = client.cookies[view_access.COOKIE_NAME]

    monkeypatch.setattr(view_access.time, "time", lambda: now + 10 * 24 * 60 * 60)
    result = client.get("/api/session")
    renewed_token = client.cookies[view_access.COOKIE_NAME]
    assert result.json() == {"authenticated": True}
    assert renewed_token != original_token
    cookie = result.headers["set-cookie"].lower()
    assert "max-age=2592000" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie

    monkeypatch.setattr(view_access.time, "time", lambda: now + 31 * 24 * 60 * 60)
    assert client.get("/private").status_code == 200


def test_signing_key_is_race_safe_private_and_survives_reload(tmp_path, monkeypatch):
    """Concurrent processes can share one private key and reloads keep cookies valid."""
    settings = SimpleNamespace(
        view_password="viewer-secret", ops_token="admin-secret", data_dir=tmp_path
    )
    monkeypatch.setattr(view_access, "load_settings", lambda: settings)
    with ThreadPoolExecutor(max_workers=8) as executor:
        secrets = list(executor.map(lambda _: view_access._signing_secret(), range(8)))
    assert len(set(secrets)) == 1
    key_path = tmp_path / view_access.SIGNING_KEY_FILE
    assert len(key_path.read_bytes()) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    now = 2_000_000_000.0
    token = view_access._session_token(now)
    reloaded = importlib.reload(view_access)
    monkeypatch.setattr(reloaded, "load_settings", lambda: settings)
    assert reloaded._valid_session(token, now + 1)


def test_unicode_password_can_create_a_session(tmp_path, monkeypatch):
    """Configured non-ASCII passwords compare as UTF-8 bytes without server errors."""
    settings = SimpleNamespace(
        view_password="pässword-密碼",
        ops_token="admin-secret",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(view_access, "load_settings", lambda: settings)
    app = FastAPI()
    app.include_router(view_access.router)
    client = TestClient(app, base_url="https://green.example")

    result = login(client, password="pässword-密碼")
    assert result.status_code == 200
    assert client.get("/api/session").json() == {"authenticated": True}


def test_viewer_cookie_does_not_authorize_admin_route(viewer_app):
    """A viewer session never substitutes for the separate Ops bearer token."""
    client = TestClient(viewer_app, base_url="https://green.example")
    assert login(client).status_code == 200
    assert (
        client.post("/admin", headers={"Origin": "https://green.example"}).status_code
        == 401
    )
    result = client.post(
        "/admin",
        headers={
            "Origin": "https://green.example",
            "Authorization": "Bearer admin-secret",
        },
    )
    assert result.json() == {"admin": True}


def test_empty_password_allows_local_read_setup(monkeypatch):
    """An intentionally empty viewer password leaves local read routes open."""
    monkeypatch.setattr(
        view_access,
        "load_settings",
        lambda: SimpleNamespace(view_password=""),
    )
    app = FastAPI()
    app.include_router(view_access.router)

    @app.get("/private", dependencies=[Depends(view_access.authorize_view)])
    def private():
        return {"visible": True}

    client = TestClient(app)
    assert client.get("/private").json() == {"visible": True}
    assert client.get("/api/session").json() == {"authenticated": True}
    result = client.post("/api/login", json={"password": ""})
    assert result.json() == {"authenticated": True}
    assert "set-cookie" not in result.headers
