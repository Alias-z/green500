"""Verify the password-protected report catalog and inert original-file boundary."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from green500 import catalog, db, view_access, web
from green500.config import load_settings
from green500.storage import save_bytes

TEST_COMPANY_CIK = "9999999998"
TEST_VIEW_PASSWORD = "catalog-viewer-test-password"
TEST_OPS_TOKEN = "catalog-ops-test-token"


def remove_unreferenced_object(settings, digest: str) -> None:
    """Remove one test object only after every owned database reference is gone."""
    with db.connect(settings) as connection:
        references = connection.execute(
            """SELECT
            (SELECT count(*) FROM documents WHERE sha256=%s OR parsed_sha256=%s) +
            (SELECT count(*) FROM extractions
             WHERE input_sha256=%s OR response_sha256=%s OR result_sha256=%s) AS count""",
            (digest, digest, digest, digest, digest),
        ).fetchone()["count"]
    if references:
        return
    path = settings.data_dir / "objects" / digest[:2] / digest
    if path.exists():
        path.unlink()


@pytest.fixture
def catalog_records():
    """Create distinct catalog, private-document and model-receipt records, then clean them."""
    settings = load_settings()
    run_id = str(uuid.uuid4())
    with db.connect(settings) as connection:
        if connection.execute(
            "SELECT 1 FROM companies WHERE cik=%s", (TEST_COMPANY_CIK,)
        ).fetchone():
            raise RuntimeError("The catalog test company CIK is already in use.")
    urls = {
        "html": f"https://example.com/green500-catalog-test/{run_id}/report.html",
        "pdf": f"https://example.com/green500-catalog-test/{run_id}/report.pdf",
        "discovered": (
            f"https://example.com/green500-catalog-test/{run_id}/discovered.pdf"
        ),
        "private": f"https://example.com/green500-catalog-test/{run_id}/private.txt",
    }
    bodies = {
        "html": (
            b"<!doctype html><script>window.catalog_test_executed=true</script>"
            + f"<p>Public issuer report {run_id}.</p>".encode()
        ),
        "pdf": (
            f"%PDF-1.4\n% Green500 catalog boundary fixture {run_id}\n%%EOF\n".encode()
        ),
        "private": f"Unreviewed private source document {run_id}.".encode(),
        "model": (f'{{"provider":"test","private_model_receipt":"{run_id}"}}'.encode()),
    }
    digests = {
        name: save_bytes(settings.data_dir, body) for name, body in bodies.items()
    }
    task_id = None
    extraction_id = None
    with db.connect(settings) as connection:
        connection.execute(
            """INSERT INTO companies(cik,name,sector,symbols,is_current)
            VALUES (%s,'Catalog boundary test company','Testing',ARRAY['CATALOG'],true)""",
            (TEST_COMPANY_CIK,),
        )
        html_document_id = db.record_document(
            connection,
            company_cik=TEST_COMPANY_CIK,
            url=urls["html"],
            final_url=urls["html"],
            title="Reviewed HTML issuer report",
            kind="report",
            digest=digests["html"],
            byte_count=len(bodies["html"]),
            content_type="text/html; charset=utf-8",
        )
        pdf_document_id = db.record_document(
            connection,
            company_cik=TEST_COMPANY_CIK,
            url=urls["pdf"],
            final_url=urls["pdf"],
            title="Reviewed PDF issuer report",
            kind="report",
            digest=digests["pdf"],
            byte_count=len(bodies["pdf"]),
            content_type="application/pdf",
        )
        checked_at = datetime.now(UTC)
        pdf_review = {
            "status": "latest_verified",
            "document_sha256": digests["pdf"],
            "checked_at": checked_at.isoformat(),
            "valid_until": (checked_at + timedelta(days=7)).isoformat(),
            "report_family": "annual_environment_report",
            "scope": "company_publisher_series",
            "evidence_urls": [urls["pdf"]],
            "publication_date": "2026-05-01",
            "report_period": {"label": "Fiscal 2025"},
        }
        private_document_id = db.record_document(
            connection,
            company_cik=TEST_COMPANY_CIK,
            url=urls["private"],
            final_url=urls["private"],
            title="Unreviewed document",
            kind="report",
            digest=digests["private"],
            byte_count=len(bodies["private"]),
            content_type="text/plain",
        )
        for title, url, categories, discovered_via in (
            (
                "Reviewed HTML issuer report",
                urls["html"],
                ["financial_report"],
                "agent",
            ),
            (
                "Reviewed PDF issuer report",
                urls["pdf"],
                ["environment_report"],
                "agent",
            ),
            (
                "Profile-discovered report without an original",
                urls["discovered"],
                ["social_employee", "financial_targets", "climate_targets"],
                "company_profile",
            ),
        ):
            connection.execute(
                """INSERT INTO report_sources
                (company_cik,url,title,categories,reporting_period,discovered_via,evidence,review)
                VALUES (%s,%s,%s,%s,'Catalog test period',%s,%s,%s)""",
                (
                    TEST_COMPANY_CIK,
                    url,
                    title,
                    categories,
                    discovered_via,
                    Jsonb([{"section": "Catalog test evidence"}]),
                    Jsonb(pdf_review if url == urls["pdf"] else {}),
                ),
            )

    try:
        with db.connect(settings) as connection:
            # Read-only catalog fixtures must never enter the deployed worker queue.
            task_id = connection.execute(
                "INSERT INTO tasks(kind,company_cik,input,input_key,status,finished_at) VALUES ('extract',%s,%s,%s,'succeeded',now()) RETURNING id",
                (TEST_COMPANY_CIK, Jsonb({"document_id":private_document_id,"catalog_boundary_test":run_id}), "catalog-boundary-"+run_id),
            ).fetchone()["id"]
            extraction_id = connection.execute(
                """INSERT INTO extractions
                (task_id,document_id,input_sha256,response_sha256,result_sha256,
                 model,prompt_version,chunk_number,status)
                VALUES (%s,%s,%s,%s,%s,'catalog-test-model','catalog-test-v1',1,'succeeded')
                RETURNING id""",
                (
                    task_id,
                    private_document_id,
                    digests["private"],
                    digests["model"],
                    digests["model"],
                ),
            ).fetchone()["id"]
        yield {
            "settings": settings,
            "urls": urls,
            "digests": digests,
            "html_document_id": html_document_id,
            "pdf_document_id": pdf_document_id,
            "private_document_id": private_document_id,
            "task_id": task_id,
            "extraction_id": extraction_id,
        }
    finally:
        with db.connect(settings) as connection:
            connection.execute(
                "DELETE FROM metrics WHERE extraction_id IN (SELECT id FROM extractions WHERE task_id=%s)",
                (task_id,),
            )
            connection.execute("DELETE FROM extractions WHERE task_id=%s", (task_id,))
            connection.execute("DELETE FROM task_attempts WHERE task_id=%s", (task_id,))
            connection.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
            connection.execute(
                "DELETE FROM report_sources WHERE company_cik=%s", (TEST_COMPANY_CIK,)
            )
            connection.execute(
                "DELETE FROM documents WHERE company_cik=%s", (TEST_COMPANY_CIK,)
            )
            connection.execute(
                "DELETE FROM companies WHERE cik=%s", (TEST_COMPANY_CIK,)
            )
        for digest in digests.values():
            remove_unreferenced_object(settings, digest)


@pytest.fixture
def secured_catalog_client(monkeypatch, catalog_records):
    """Use the real database with distinct viewer and administrator credentials."""
    secured_settings = replace(
        catalog_records["settings"],
        view_password=TEST_VIEW_PASSWORD,
        ops_token=TEST_OPS_TOKEN,
    )
    monkeypatch.setattr(view_access, "load_settings", lambda: secured_settings)
    monkeypatch.setattr(web, "load_settings", lambda: secured_settings)
    monkeypatch.setattr(
        catalog, "load_settings", lambda: secured_settings, raising=False
    )
    view_access._FAILED_LOGINS.clear()
    client = TestClient(web.app, base_url="https://green500.test")
    yield client, catalog_records
    view_access._FAILED_LOGINS.clear()


def login_viewer(client: TestClient) -> None:
    """Create one same-origin viewer session for catalog requests."""
    result = client.post(
        "/api/login",
        json={"password": TEST_VIEW_PASSWORD},
        headers={"Origin": "https://green500.test"},
    )
    assert result.status_code == 200


def test_empty_view_password_allows_catalog_files_but_not_admin(
    monkeypatch, catalog_records
):
    """An open read-only catalog still keeps every mutation behind the Ops token."""
    settings = replace(
        catalog_records["settings"], view_password="", ops_token=TEST_OPS_TOKEN
    )
    monkeypatch.setattr(view_access, "load_settings", lambda: settings)
    monkeypatch.setattr(web, "load_settings", lambda: settings)
    monkeypatch.setattr(catalog, "load_settings", lambda: settings, raising=False)
    client = TestClient(web.app, base_url="https://green500.test")

    assert client.get("/api/catalog").status_code == 200
    document_id = catalog_records["pdf_document_id"]
    assert client.get(f"/api/catalog/files/{document_id}/view").status_code == 200
    assert (
        client.post(
            "/api/actions",
            json={"action": "collect_financial", "company_cik": TEST_COMPANY_CIK},
            headers={"Origin": "https://green500.test"},
        ).status_code
        == 401
    )


def test_viewer_login_grants_catalog_but_not_admin_actions(secured_catalog_client):
    """The viewer password can read reports but never substitutes for the Ops token."""
    client, _ = secured_catalog_client
    assert client.get("/api/catalog").status_code == 401
    assert client.get(f"/api/catalog/{TEST_COMPANY_CIK}").status_code == 401

    login_viewer(client)
    assert client.get("/api/catalog").status_code == 200
    assert client.get(f"/api/catalog/{TEST_COMPANY_CIK}").status_code == 200
    action = {"action": "collect_financial", "company_cik": TEST_COMPANY_CIK}
    assert (
        client.post(
            "/api/actions",
            json=action,
            headers={"Origin": "https://green500.test"},
        ).status_code
        == 401
    )


def test_catalog_counts_only_downloaded_originals_as_available(secured_catalog_client):
    """A profile-discovered URL remains pending until a matching original is stored."""
    client, records = secured_catalog_client
    login_viewer(client)
    result = client.get("/api/catalog")
    assert result.status_code == 200
    company = next(
        row for row in result.json()["companies"] if row["cik"] == TEST_COMPANY_CIK
    )

    assert company["source_count"] == 3
    assert company["coverage"]["financial_report"] == {
        "available": 1,
        "pending": 0,
        "failed": 0,
        "latest_verified": 0,
        "newer_available": 0,
        "unknown": 1,
    }
    assert company["coverage"]["environment_report"] == {
        "available": 1,
        "pending": 0,
        "failed": 0,
        "latest_verified": 1,
        "newer_available": 0,
        "unknown": 0,
    }
    for category in ("social_employee", "financial_targets", "climate_targets"):
        assert company["coverage"][category] == {
            "available": 0,
            "pending": 1,
            "failed": 0,
            "latest_verified": 0,
            "newer_available": 0,
            "unknown": 0,
        }

    detail = client.get(f"/api/catalog/{TEST_COMPANY_CIK}")
    assert detail.status_code == 200
    sources = {source["url"]: source for source in detail.json()["sources"]}
    assert sources[records["urls"]["html"]]["collection_status"] == "available"
    assert sources[records["urls"]["html"]]["freshness"]["status"] == "unknown"
    assert sources[records["urls"]["html"]]["freshness"]["reason"] == "not_reviewed"
    assert (
        sources[records["urls"]["html"]]["document_id"] == records["html_document_id"]
    )
    assert sources[records["urls"]["discovered"]]["collection_status"] == (
        "not_downloaded"
    )
    assert sources[records["urls"]["discovered"]]["document_id"] is None
    assert sources[records["urls"]["pdf"]]["freshness"]["status"] == ("latest_verified")
    assert (
        sources[records["urls"]["pdf"]]["review"]["document_sha256"]
        == (records["digests"]["pdf"])
    )
    assert records["digests"]["model"] not in detail.text


def test_newer_candidate_does_not_remove_downloaded_original(secured_catalog_client):
    """A blocked newer edition changes freshness while the older original stays readable."""
    client, records = secured_catalog_client
    checked_at = datetime.now(UTC)
    review = {
        "status": "newer_available",
        "document_sha256": records["digests"]["html"],
        "checked_at": checked_at.isoformat(),
        "valid_until": (checked_at + timedelta(days=7)).isoformat(),
        "report_family": "annual_financial_report",
        "scope": "company_publisher_series",
        "evidence_urls": [records["urls"]["html"]],
        "publication_date": "2025-05-01",
        "report_period": {"label": "Fiscal 2024"},
        "newer_candidate": {
            "url": records["urls"]["html"] + "?edition=2026",
            "title": "Newer annual report",
            "publication_date": "2026-05-01",
            "report_period": {"label": "Fiscal 2025"},
            "download_status": "blocked",
            "block_reason": "Publisher blocked automated retrieval.",
        },
    }
    with db.connect(records["settings"]) as connection:
        connection.execute(
            "UPDATE report_sources SET review=%s WHERE company_cik=%s AND url=%s",
            (Jsonb(review), TEST_COMPANY_CIK, records["urls"]["html"]),
        )

    login_viewer(client)
    catalog_response = client.get("/api/catalog").json()
    company = next(
        row for row in catalog_response["companies"] if row["cik"] == TEST_COMPANY_CIK
    )
    assert company["coverage"]["financial_report"]["available"] == 1
    assert company["coverage"]["financial_report"]["newer_available"] == 1
    assert company["coverage"]["financial_report"]["latest_verified"] == 0

    detail = client.get(f"/api/catalog/{TEST_COMPANY_CIK}").json()
    source = next(
        item for item in detail["sources"] if item["url"] == records["urls"]["html"]
    )
    assert source["freshness"]["status"] == "newer_available"
    assert source["freshness"]["newer_candidate"]["download_status"] == "blocked"
    assert (
        client.get(f"/api/catalog/files/{records['html_document_id']}/view").status_code
        == 200
    )


def test_catalog_html_view_is_plain_text_and_download_is_attachment(
    secured_catalog_client,
):
    """Stored HTML stays inert in both viewer file modes."""
    client, records = secured_catalog_client
    login_viewer(client)
    document_id = records["html_document_id"]

    viewed = client.get(f"/api/catalog/files/{document_id}/view")
    assert viewed.status_code == 200
    assert viewed.headers["content-type"].startswith("text/plain")
    assert "sandbox" in viewed.headers["content-security-policy"].lower()
    assert viewed.headers["x-content-type-options"] == "nosniff"
    assert viewed.content.startswith(b"<!doctype html>")

    downloaded = client.get(f"/api/catalog/files/{document_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.content == viewed.content


def test_catalog_pdf_view_is_inline(secured_catalog_client):
    """A reviewed PDF can render inline while its download remains an attachment."""
    client, records = secured_catalog_client
    login_viewer(client)
    document_id = records["pdf_document_id"]

    viewed = client.get(f"/api/catalog/files/{document_id}/view")
    assert viewed.status_code == 200
    assert viewed.headers["content-type"].startswith("application/pdf")
    assert viewed.headers["content-disposition"].startswith("inline;")
    assert viewed.content.startswith(b"%PDF-")

    downloaded = client.get(f"/api/catalog/files/{document_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.content == viewed.content


def test_catalog_cannot_serve_unreviewed_files_or_model_receipts(
    secured_catalog_client,
):
    """Viewer access excludes unreviewed documents and Ops-only model receipts."""
    client, records = secured_catalog_client
    login_viewer(client)

    private_document_id = records["private_document_id"]
    assert (
        client.get(f"/api/catalog/files/{private_document_id}/view").status_code == 404
    )
    assert (
        client.get(f"/api/catalog/files/{private_document_id}/download").status_code
        == 404
    )
    assert client.get("/api/catalog/files/9223372036854775807/view").status_code == 404

    model_receipt = client.get("/api/objects/" + records["digests"]["model"])
    assert model_receipt.status_code == 401
    with db.connect(records["settings"]) as connection:
        assert (
            connection.execute(
                "SELECT response_sha256 FROM extractions WHERE id=%s",
                (records["extraction_id"],),
            ).fetchone()["response_sha256"]
            == records["digests"]["model"]
        )
