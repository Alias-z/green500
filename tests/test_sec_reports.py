"""Pure checks for current SEC report selection and proxy classification."""

import json
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace

import pytest

from green500 import catalog, sec_reports
from green500.sec_reports import (
    build_sec_source_review,
    parse_latest_report_sources,
    partition_report_sources_for_resume,
    proxy_employee_content,
)
from green500.storage import read_bytes, save_bytes


def submissions_body(rows, cik="0000789019", name="MICROSOFT CORPORATION"):
    """Encode recent filings in the SEC's compact column-oriented shape."""
    fields = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "primaryDocument",
    )
    recent = {field: [row[field] for row in rows] for field in fields}
    return json.dumps(
        {"cik": cik, "name": name, "filings": {"recent": recent}}
    ).encode()


def filing(accession, form, filed, report_date, document):
    """Create one complete recent-filings row."""
    return {
        "accessionNumber": accession,
        "form": form,
        "filingDate": filed,
        "reportDate": report_date,
        "primaryDocument": document,
    }


def test_selects_only_latest_annual_quarterly_and_proxy_documents():
    """Old filings and unrelated 8-K records cannot expand current collection."""
    rows = [
        filing(
            "0000000001-25-000001",
            "10-K",
            "2025-07-30",
            "2025-06-30",
            "old-annual.htm",
        ),
        filing(
            "0000000001-26-000010",
            "10-K",
            "2026-07-29",
            "2026-06-30",
            "current-annual.htm",
        ),
        filing(
            "0000000001-26-000007",
            "10-Q",
            "2026-04-29",
            "2026-03-31",
            "old-quarter.htm",
        ),
        filing(
            "0000000001-26-000011",
            "10-Q",
            "2026-08-01",
            "2026-06-30",
            "current-quarter.htm",
        ),
        filing(
            "0000000001-26-000012",
            "DEF 14A",
            "2026-08-10",
            "2026-06-30",
            "current-proxy.htm",
        ),
        filing(
            "0000000001-26-000013",
            "8-K",
            "2026-08-20",
            "2026-08-20",
            "earnings-event.htm",
        ),
    ]

    parsed = parse_latest_report_sources(submissions_body(rows), "0000789019")
    by_type = {source["document_type"]: source for source in parsed["sources"]}

    assert set(by_type) == {"annual", "quarterly", "proxy"}
    assert parsed["missing_forms"] == []
    assert by_type["annual"]["url"].endswith(
        "/789019/000000000126000010/current-annual.htm"
    )
    assert by_type["quarterly"]["url"].endswith(
        "/789019/000000000126000011/current-quarter.htm"
    )
    assert by_type["proxy"]["url"].endswith(
        "/789019/000000000126000012/current-proxy.htm"
    )
    assert by_type["proxy"]["categories"] == ["financial_report"]
    assert by_type["annual"]["evidence"][0]["form"] == "10-K"
    assert by_type["annual"]["filed_date"] == "2026-07-29"
    assert by_type["annual"]["report_date"] == "2026-06-30"


def test_latest_supported_foreign_annual_form_is_selected():
    """A current 20-F is a supported annual original when no 10-K is filed."""
    rows = [
        filing(
            "0000000002-26-000001",
            "20-F",
            "2026-03-20",
            "2025-12-31",
            "issuer-2025.htm",
        )
    ]
    parsed = parse_latest_report_sources(
        submissions_body(rows, cik="0000000002", name="FOREIGN ISSUER"),
        "2",
    )

    assert len(parsed["sources"]) == 1
    assert parsed["sources"][0]["evidence"][0]["form"] == "20-F"
    assert parsed["missing_forms"] == ["quarterly", "proxy"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'{"cik":', "malformed or truncated"),
        (
            submissions_body([], cik="0000320193", name="Apple Inc."),
            "returned CIK 0000320193",
        ),
        (
            json.dumps(
                {
                    "cik": "0000789019",
                    "name": "MICROSOFT CORPORATION",
                    "filings": {
                        "recent": {
                            "accessionNumber": ["0000000001-26-000001"],
                            "form": ["10-K"],
                            "filingDate": [],
                            "reportDate": ["2025-12-31"],
                            "primaryDocument": ["report.htm"],
                        }
                    },
                }
            ).encode(),
            "different lengths",
        ),
        (
            submissions_body(
                [
                    filing(
                        "0000000001-26-000001",
                        "10-K",
                        "2026-02-01",
                        "2025-12-31",
                        "../private.htm",
                    )
                ]
            ),
            "unsafe primary document",
        ),
    ],
)
def test_invalid_submissions_fail_closed(body, message):
    """Truncation, wrong entities, broken columns and unsafe paths are rejected."""
    with pytest.raises(ValueError, match=message):
        parse_latest_report_sources(body, "0000789019")


def test_proxy_requires_employee_pay_or_labor_content():
    """Executive compensation alone does not establish employee-social coverage."""
    confirmed = proxy_employee_content(
        b"""<html><h2>CEO Pay Ratio</h2><p>The median employee annual total
        compensation was calculated for the fiscal year.</p><p>Our labor relations
        programs cover collective bargaining.</p></html>"""
    )
    unconfirmed = proxy_employee_content(
        b"<html><h2>Executive Compensation</h2><p>Named executive officers.</p></html>"
    )

    assert confirmed == [
        "pay ratio",
        "median employee compensation",
        "labor relations",
    ]
    assert unconfirmed == []


def test_resume_reuses_verified_proxy_without_scheduling_or_reclassifying_it():
    """A verified immutable URL stays out of downloads and keeps its stored profile untouched."""
    proxy = {
        "url": "https://www.sec.gov/Archives/edgar/data/1/proxy.htm",
        "document_type": "proxy",
        "categories": ["financial_report"],
    }
    annual = {
        "url": "https://www.sec.gov/Archives/edgar/data/1/annual.htm",
        "document_type": "annual",
        "categories": ["financial_report"],
    }
    stored = {"document_id": 42, "sha256": "a" * 64}

    reused, downloads = partition_report_sources_for_resume(
        [proxy, annual], {proxy["url"]: stored}
    )

    assert reused == [(proxy, stored)]
    assert downloads == [annual]
    assert proxy["categories"] == ["financial_report"]


def test_sec_review_uses_listing_hash_and_preserves_curated_metadata():
    """A SEC refresh scopes freshness without discarding a prior ESG assessment or note."""
    source = {
        "document_type": "annual",
        "filed_date": "2026-02-20",
        "report_date": "2025-12-31",
    }
    listing = {
        "sha256": "b" * 64,
        "checked_at": "2026-09-12T12:00:00+00:00",
        "evidence_url": "https://data.sec.gov/submissions/CIK0000789019.json",
    }
    previous = {
        "notes": ["Curated environmental classification."],
        "category_assessments": {
            "environment_report": {
                "scope": "company_environment_series",
                "target_status": "unknown",
            }
        },
    }

    review = build_sec_source_review(
        source,
        ["financial_report", "environment_report"],
        "a" * 64,
        listing,
        previous,
    )

    assert review["document_sha256"] == "a" * 64
    assert review["report_family"] == "sec_annual_filing"
    assert review["publication_date"] == "2026-02-20"
    assert review["report_period"] == {
        "start": None,
        "end": "2025-12-31",
        "label": None,
    }
    assert review["evidence_urls"] == [listing["evidence_url"]]
    assert review["notes"] == [
        "Curated environmental classification.",
        "SEC submissions listing SHA-256: " + "b" * 64,
    ]
    assert "environment_report" in review["category_assessments"]
    assert "financial_report" in review["category_assessments"]
    assert (
        datetime.fromisoformat(review["valid_until"])
        - datetime.fromisoformat(review["checked_at"])
    ).days == 7


def test_sec_review_does_not_make_unconfirmed_proxy_or_esg_categories_latest():
    """SEC series evidence applies only to the filing category it can establish."""
    listing = {
        "sha256": "b" * 64,
        "checked_at": "2026-09-12T12:00:00+00:00",
        "evidence_url": "https://data.sec.gov/submissions/CIK0000789019.json",
    }
    proxy_review = build_sec_source_review(
        {
            "document_type": "proxy",
            "filed_date": "2026-04-01",
            "report_date": None,
        },
        ["financial_report"],
        "a" * 64,
        listing,
    )
    source = {
        "freshness": proxy_review | {"reason": "valid", "label": "Latest verified"}
    }
    assert catalog.category_freshness_status(source, "financial_report") == "unknown"

    annual_review = build_sec_source_review(
        {
            "document_type": "annual",
            "filed_date": "2026-02-20",
            "report_date": "2025-12-31",
        },
        ["financial_report", "environment_report", "climate_targets"],
        "a" * 64,
        listing,
        {
            "category_assessments": {
                "environment_report": {
                    "scope": "company_environment_series",
                    "target_status": "unknown",
                },
                "climate_targets": {
                    "scope": "company_climate_targets",
                    "target_status": "achieved",
                },
            }
        },
    )
    source["freshness"] = annual_review | {
        "reason": "valid",
        "label": "Latest verified",
    }
    assert catalog.category_freshness_status(source, "financial_report") == (
        "latest_verified"
    )
    assert catalog.category_freshness_status(source, "environment_report") == "unknown"
    assert catalog.category_freshness_status(source, "climate_targets") == "unknown"


def test_submissions_listing_is_saved_as_immutable_evidence(tmp_path, monkeypatch):
    """The exact JSON selected for latest filings is retained under its SHA-256."""
    body = b'{"cik":"0000789019","filings":{"recent":{}}}'
    calls = {}

    class Connection:
        def execute(self, query, parameters):
            calls["update"] = (query, parameters)

    monkeypatch.setattr(
        sec_reports.db, "connect", lambda _settings: nullcontext(Connection())
    )
    monkeypatch.setattr(
        sec_reports.db,
        "require_active_task",
        lambda _connection, task_id: calls.setdefault("task_id", task_id),
    )

    def record_document(_connection, **document):
        calls["document"] = document
        return 91

    monkeypatch.setattr(sec_reports.db, "record_document", record_document)
    settings = SimpleNamespace(data_dir=tmp_path, max_document_bytes=1024)
    url = "https://data.sec.gov/submissions/CIK0000789019.json"

    listing = sec_reports._save_submissions_listing(
        settings,
        42,
        "0000789019",
        "MICROSOFT CORPORATION",
        url,
        url,
        body,
        "application/json",
    )

    assert calls["task_id"] == 42
    assert calls["document"]["kind"] == "directory"
    assert calls["document"]["url"] == url
    assert listing["document_id"] == 91
    assert read_bytes(tmp_path, listing["sha256"]) == body
    assert "source_key='sec_submissions'" in calls["update"][0]


def test_existing_report_is_reused_only_while_its_stored_bytes_match_sha256(
    tmp_path, monkeypatch
):
    """A missing or changed raw object cannot suppress the SEC network request."""
    body = b"immutable SEC filing"
    digest = save_bytes(tmp_path, body)
    row = {
        "id": 42,
        "sha256": digest,
        "byte_count": len(body),
        "content_type": "text/html",
        "final_url": "https://www.sec.gov/Archives/edgar/data/1/annual.htm",
    }

    class QueryResult:
        def fetchall(self):
            return [row]

    class Connection:
        def execute(self, _query, _parameters):
            return QueryResult()

    monkeypatch.setattr(
        sec_reports.db, "connect", lambda _settings: nullcontext(Connection())
    )
    settings = SimpleNamespace(data_dir=tmp_path, max_document_bytes=1024)

    assert sec_reports._verified_existing_report(
        settings, "0000000001", row["final_url"]
    ) == {
        "document_id": 42,
        "sha256": digest,
        "byte_count": len(body),
        "content_type": "text/html",
        "final_url": row["final_url"],
    }

    (tmp_path / "objects" / digest[:2] / digest).write_bytes(b"changed")
    assert (
        sec_reports._verified_existing_report(settings, "0000000001", row["final_url"])
        is None
    )
