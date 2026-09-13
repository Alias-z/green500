"""Verify authenticated Ops actions against the dedicated application database."""

from fastapi.testclient import TestClient

from green500 import db
from green500.config import load_settings
from green500.web import app


def test_ops_requires_token_and_lists_companies():
    """The browser API exposes company progress only after authentication."""
    settings = load_settings()
    client = TestClient(app)
    assert client.get("/api/companies").status_code == 401
    headers = {"Authorization": "Bearer " + settings.ops_token}
    result = client.get("/api/companies?search=MSFT", headers=headers)
    assert result.status_code == 200
    rows = result.json()["companies"]
    assert len(rows) == 1 and rows[0]["cik"] == "0000789019"
    assert "llm_api_key" not in result.text and settings.llm_api_key not in result.text


def test_ops_rejects_private_source_and_cross_origin_write():
    """Source selection cannot fetch local services and a foreign page cannot issue actions."""
    settings = load_settings()
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + settings.ops_token}
    action = {
        "action": "collect_source",
        "company_cik": "0000789019",
        "url": "http://127.0.0.1/",
        "source_kind": "report",
    }
    assert client.post("/api/actions", headers=headers, json=action).status_code == 400
    headers["Origin"] = "https://unrelated.example"
    assert (
        client.post("/api/pause", headers=headers, json={"is_paused": True}).status_code
        == 403
    )


def test_pause_is_durable_and_reversible():
    """Pause changes admission without rewriting completed work."""
    settings = load_settings()
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + settings.ops_token}
    with db.connect(settings) as conn:
        before = conn.execute("SELECT is_paused FROM controls WHERE id=1").fetchone()[
            "is_paused"
        ]
    try:
        assert (
            client.post(
                "/api/pause", headers=headers, json={"is_paused": True}
            ).status_code
            == 200
        )
        assert (
            client.get("/api/companies", headers=headers).json()["control"]["is_paused"]
            is True
        )
        assert db.claim(settings) is None
    finally:
        client.post("/api/pause", headers=headers, json={"is_paused": before})


def test_original_source_is_served_as_inert_download():
    """Acquired HTML cannot execute on the authenticated Ops origin."""
    settings = load_settings()
    client = TestClient(app)
    with db.connect(settings) as conn:
        document = conn.execute(
            "SELECT sha256 FROM documents WHERE company_cik='0000789019' AND content_type LIKE 'text/html%' LIMIT 1"
        ).fetchone()
    result = client.get(
        "/api/objects/" + document["sha256"],
        headers={"Authorization": "Bearer " + settings.ops_token},
    )
    assert result.status_code == 200
    assert result.headers["content-type"] == "application/octet-stream"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.headers["content-disposition"].startswith("attachment;")


def test_retry_preserves_original_task():
    """A repeated queued retry is deduplicated and does not alter its failed predecessor."""
    settings = load_settings()
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + settings.ops_token}
    original = db.enqueue(
        settings,
        "collect_source",
        "0000789019",
        {"url": "https://example.com/retry-test", "source_kind": "report"},
    )
    db.claim(settings, original)
    db.finish(settings, original, "failed", error="Intentional test failure")
    retry_id = None
    try:
        result = client.post(f"/api/tasks/{original}/retry", headers=headers, json={})
        assert result.status_code == 200
        retry_id = result.json()["task_id"]
        assert retry_id != original
        repeated = client.post(f"/api/tasks/{original}/retry", headers=headers, json={})
        assert repeated.json()["task_id"] == retry_id
        assert db.get_task(settings, original)["status"] == "failed"
    finally:
        with db.connect(settings) as conn:
            ids = [original] + ([retry_id] if retry_id else [])
            conn.execute("DELETE FROM task_attempts WHERE task_id=ANY(%s)", (ids,))
            conn.execute("DELETE FROM tasks WHERE id=ANY(%s)", (ids,))
