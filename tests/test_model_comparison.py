"""Verify the comparison API's access and published snapshot boundaries."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from green500 import model_comparison as comparison
from green500 import view_access


@pytest.fixture
def client(monkeypatch, tmp_path):
    settings = SimpleNamespace(view_password="comparison-test", data_dir=tmp_path)
    monkeypatch.setattr(view_access, "load_settings", lambda: settings)
    app = FastAPI()
    app.include_router(view_access.router)
    app.include_router(comparison.router)
    view_access._FAILED_LOGINS.clear()
    return TestClient(app, base_url="https://green.example")


def sign_in(client):
    assert (
        client.post("/api/login", json={"password": "comparison-test"}).status_code
        == 200
    )


def test_renewable_scenario_request_has_no_artificial_maximum():
    request = comparison.RenewableScenarioRequest(
        renewable_multiplier=1_000_000,
        snapshot_id="a" * 64,
    )
    assert request.renewable_multiplier == 1_000_000


def test_comparison_requires_viewer_and_same_origin(client, monkeypatch):
    monkeypatch.setattr(
        comparison, "_snapshot", lambda: {"summary": {"company_count": 500}}
    )
    assert client.get("/api/model-comparison/run").status_code == 401
    sign_in(client)
    assert client.get("/api/model-comparison/run").json() == {"company_count": 500}
    response = client.post(
        "/api/model-comparison/compare",
        json={"company_ids": ["0000001800"]},
        headers={"Origin": "https://other.example"},
    )
    assert response.status_code == 403
    response = client.post(
        "/api/model-comparison/target-suggestions",
        json={
            "company_id": "0000001800",
            "desired_score": 50,
            "snapshot_id": "a" * 64,
        },
        headers={"Origin": "https://other.example"},
    )
    assert response.status_code == 403
    response = client.post(
        "/api/model-comparison/renewable-scenario",
        json={"snapshot_id": "a" * 64, "renewable_multiplier": 1.1},
        headers={"Origin": "https://other.example"},
    )
    assert response.status_code == 403


def test_requests_reject_unknown_inputs_and_nonfinite_weights(client):
    sign_in(client)
    for body in (
        {"company_ids": ["0000001800"], "model_run": "/tmp/untrusted"},
        {"company_ids": [], "target": "esg"},
        {"company_ids": ["0000001800"], "weights": {"environmental": "NaN"}},
        {
            "company_ids": ["0000001800"],
            "adjustments": [
                {
                    "feature_name": "env_scope_1_tco2e",
                    "operation": "add",
                    "value": "Infinity",
                }
            ],
        },
    ):
        assert (
            client.post("/api/model-comparison/compare", json=body).status_code == 422
        )


def test_changed_snapshot_rejected_before_model_load(client, monkeypatch):
    sign_in(client)
    monkeypatch.setattr(comparison, "_snapshot", lambda: {"snapshot_id": "a" * 64})
    monkeypatch.setattr(
        comparison,
        "_service",
        lambda _: pytest.fail("Stale request reached model loading"),
    )
    result = client.post(
        "/api/model-comparison/compare",
        json={"company_ids": ["0000001800"], "snapshot_id": "b" * 64},
    )
    assert result.status_code == 409


def test_duplicate_companies_and_bounded_concurrency(client):
    sign_in(client)
    assert (
        client.post(
            "/api/model-comparison/compare",
            json={"company_ids": ["0000001800", "0000001800"]},
        ).status_code
        == 422
    )
    assert comparison._requests.acquire(False)
    assert comparison._requests.acquire(False)
    try:
        response = client.post(
            "/api/model-comparison/compare", json={"company_ids": ["0000001800"]}
        )
        assert response.status_code == 429
    finally:
        comparison._requests.release()
        comparison._requests.release()


def test_rank_ties_use_same_deterministic_company_order():
    rows = [
        {"company": {"company_cik": "2"}, "value": 10},
        {"company": {"company_cik": "1"}, "value": 10},
    ]
    assert comparison._rank_map(rows, lambda row: row["value"]) == {"1": 1, "2": 2}


def test_demo_presets_bind_visible_actions_and_four_use_cases(client, monkeypatch):
    sign_in(client)
    monkeypatch.setattr(comparison, "_snapshot", lambda: {"snapshot_id": "a" * 64})
    result = client.get("/api/model-comparison/demos")
    assert result.status_code == 200
    body = result.json()
    assert [preset["id"] for preset in body["pair_presets"]] == [
        "clean_power",
        "direct_emissions",
        "revenue_scale",
    ]
    assert all(len(preset["company_ids"]) == 2 for preset in body["pair_presets"])
    assert body["pair_presets"][0]["actions"]["increase"] == {
        "label": "+25 points per click",
        "operation": "add",
        "value": 25,
    }
    assert body["reach_preset"]["desired_score"] == 50
    assert body["ranking_preset"]["weights"] == {"environmental": 3}
    assert body["fund_preset"]["constraints"]["fund_usd"] == 1_000_000_000
    assert body["fund_preset"]["objective"] == "financial_resilience"
    assert body["fund_preset"]["sustainability_eligible_fraction"] == 0.75


def test_published_ranking_reweights_signed_categories_for_all_companies():
    categories = {
        "financial": 0.0,
        "social": 0.0,
        "environmental": 2.0,
        "climate_target": 0.0,
        "financial_target": 0.0,
    }
    values = [
        {
            "company_cik": "0000000001",
            "company_name": "Alpha",
            "ticker": "A",
            "industry": "Industrials",
            "ebm": 10.0,
            "intercept": 8.0,
            "fixed_context_contribution": 0.0,
            "category_contributions": categories,
        },
        {
            "company_cik": "0000000002",
            "company_name": "Beta",
            "ticker": "B",
            "industry": "Utilities",
            "ebm": 9.0,
            "intercept": 4.0,
            "fixed_context_contribution": 0.0,
            "category_contributions": {**categories, "environmental": 5.0},
        },
    ]
    baseline = comparison.rank_published_values(values, {})
    assert [
        (item["company"]["ticker"], item["personalized_rank"])
        for item in baseline["companies"]
    ] == [("A", 1), ("B", 2)]
    result = comparison.rank_published_values(values, {"environmental": 2.0})
    assert result["company_count"] == 2
    assert [
        (
            item["company"]["ticker"],
            item["original_rank"],
            item["personalized_rank"],
            item["rank_change"],
        )
        for item in result["companies"]
    ] == [
        ("B", 2, 1, 1),
        ("A", 1, 2, -1),
    ]
    assert result["companies"][0]["weighted_category_effect"] == 5
    assert result["companies"][0]["weighted_category_effects"]["environmental"] == 5


def test_published_ranking_rejects_changed_or_incomplete_contributions():
    with pytest.raises(ValueError, match="invalid category contributions"):
        comparison.rank_published_values(
            [
                {
                    "company_cik": "1",
                    "ebm": 1.0,
                    "intercept": 1.0,
                    "fixed_context_contribution": 0.0,
                    "category_contributions": {"environmental": 0.0},
                }
            ],
            {"environmental": 2.0},
        )


def test_evidence_links_require_exact_company_and_hash():
    from green500.model_comparison_store import _verified_evidence

    class Connection:
        def execute(self, query, parameters):
            assert "WHERE id=ANY(%s)" in query
            assert parameters == ([7],)
            return self

        def fetchall(self):
            return [{"id": 7, "company_cik": "0000001800", "sha256": "valid"}]

    row = {
        "company": {"company_cik": "0000001800"},
        "metadata": {
            "verified": {"source_document_id": 7, "source_document_sha256": "valid"},
            "different_hash": {
                "source_document_id": 7,
                "source_document_sha256": "old",
                "source_view_url": "/unverified",
            },
            "missing_id": {
                "source_document_id": None,
                "source_view_url": "/unverified",
            },
        },
    }
    result = _verified_evidence(Connection(), [row])[0]["metadata"]
    assert result["verified"]["source_view_url"] == "/api/catalog/files/7/view"
    assert result["different_hash"]["source_view_url"] is None
    assert result["different_hash"]["source_document_id"] is None
    assert not result["missing_id"].get("source_view_url")
    assert row["metadata"]["different_hash"]["source_view_url"] == "/unverified"


def test_application_pages_require_fresh_html_and_content_bound_assets():
    import hashlib
    import re

    from green500.web import app

    client = TestClient(app)
    for path in ("/", "/portfolio"):
        page = client.get(path)
        assert page.status_code == 200
        assert page.headers["cache-control"] == "private, no-store"
        assets = re.findall(
            r'(?:src|href)="(/static/[^?" ]+)\?v=([a-f0-9]{16})"', page.text
        )
        assert len(assets) == 2
        for asset_path, version in assets:
            asset = client.get(asset_path)
            assert asset.status_code == 200
            assert hashlib.sha256(asset.content).hexdigest().startswith(version)
    assert 'class="comparison-nav"' in client.get("/").text
    assert "/reports/microsoft/" not in client.get("/").text
    assert client.get("/reports/microsoft/").status_code == 404
    assert client.get("/reports/microsoft").status_code == 404
