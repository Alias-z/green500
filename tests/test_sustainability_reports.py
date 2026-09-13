"""Pure checks for bounded issuer sustainability-report discovery."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from scrapy import Request
from scrapy.http import HtmlResponse

from green500 import sustainability_reports
from green500.storage import save_bytes
from green500.sustainability_reports import (
    SustainabilityReportsSpider,
    _load_reusable_reports,
    _pending_source,
    _resume_since,
    classify_report_pages,
    directory_entries,
    issuer_website_seeds,
    match_directory_entries,
    parse_directory_profile,
    relevant_issuer_links,
    report_source_from_item,
)


def html_response(url: str, body: str) -> HtmlResponse:
    """Create a deterministic source page without a network request."""
    return HtmlResponse(url, body=body.encode(), encoding="utf-8")


def test_directory_names_match_only_when_unique():
    """The global list admits one unique normalized company name and leaves misses explicit."""
    response = html_response(
        "https://www.responsibilityreports.com/Companies",
        """
        <span class="companyName"><a href="/Company/apple-inc">Apple Inc.</a></span>
        <span class="companyName"><a href="/Company/twin-a">Twin Corp.</a></span>
        <span class="companyName"><a href="/Company/twin-b">TWIN CORP</a></span>
        """,
    )
    entries = directory_entries(response)
    matched, unmatched = match_directory_entries(
        [
            {"cik": "1", "name": "Apple, Inc."},
            {"cik": "2", "name": "Twin Corp"},
            {"cik": "3", "name": "Missing Inc"},
        ],
        entries,
    )
    assert matched["1"]["profile_url"].endswith("/Company/apple-inc")
    assert [company["cik"] for company in unmatched] == ["2", "3"]


def test_directory_profile_preserves_ticker_and_official_website():
    """Identity comes from the provider ticker and Corporation JSON-LD profile."""
    profile = {
        "@type": "Corporation",
        "name": "Example Corporation",
        "url": "https://www.example.com/responsibility",
    }
    response = html_response(
        "https://www.responsibilityreports.com/Company/example",
        '<span class="ticker_name">EXM</span>'
        f'<script type="application/ld+json">{json.dumps(profile)}</script>',
    )
    assert parse_directory_profile(response) == {
        "ticker": "EXM",
        "company_name": "Example Corporation",
        "official_url": "https://www.example.com/responsibility",
        "profile_url": response.url,
        "identity_provider": "ResponsibilityReports.com",
        "discovered_via": "issuer_site_via_responsibilityreports_directory",
    }


def test_issuer_page_accepts_relevant_external_pdf_and_same_issuer_pages():
    """A generic PDF label inherits topic context only from an official relevant page."""
    response = html_response(
        "https://www.example.com/sustainability/reports",
        """
        <title>Example 2026 Sustainability Reports</title>
        <a href="https://example.net/example-2026.pdf">View PDF</a>
        <a href="/environment/climate">Climate</a>
        <a href="https://unrelated.example/about">Sustainability article</a>
        <a href="/products">Products</a>
        """,
    )
    html_links, pdf_links = relevant_issuer_links(response, "example.com")
    assert [item["url"] for item in html_links] == [
        "https://www.example.com/environment/climate"
    ]
    assert len(pdf_links) == 1
    assert pdf_links[0]["url"] == "https://example.net/example-2026.pdf"
    assert pdf_links[0]["year"] == 2026
    assert pdf_links[0]["topics"] == ["environment", "social"]


def test_pdf_text_must_support_each_report_category():
    """ESG link wording alone cannot create environmental, employee, or target coverage."""
    classified = classify_report_pages(
        [
            (1, "This report covers Fiscal Year 2025."),
            (
                2,
                "Our Scope 1 greenhouse gas emissions inventory also reports energy and water use.",
            ),
            (
                3,
                "Employees and workers completed health and safety training across the workforce.",
            ),
            (4, "We will reduce greenhouse gas emissions by 2030."),
        ],
        "Example 2026 ESG Report",
    )
    assert classified["categories"] == [
        "environment_report",
        "social_employee",
        "climate_targets",
    ]
    assert classified["reporting_period"] == "Fiscal Year 2025"
    assert {item["category"] for item in classified["evidence"]} == {
        "environment_report",
        "social_employee",
        "climate_targets",
    }

    unsupported = classify_report_pages(
        [(1, "A general corporate overview without measured environmental data.")],
        "Example ESG Report",
    )
    assert unsupported["categories"] == []


def test_failed_download_is_only_an_unverified_pending_candidate():
    """A failed PDF request cannot claim report categories or a reporting period."""
    source = _pending_source(
        {
            "profile": {
                "profile_url": "https://www.responsibilityreports.com/Company/example",
                "company_name": "Example Corp",
                "ticker": "EXM",
                "official_url": "https://example.com",
            }
        },
        {
            "candidate": {
                "url": "https://example.net/report.pdf",
                "title": "Example Sustainability Report",
                "topics": ["environment", "social"],
                "discovered_on": "https://example.com/sustainability",
            },
            "error": "timeout",
        },
    )
    assert source["categories"] == ["environment_report", "social_employee"]
    assert source["reporting_period"] is None
    assert source["evidence"][0]["category"] == "candidate_only"


def test_resume_since_is_explicit_and_timezone_aware():
    """Resume never applies an implicit freshness window or accepts a local timestamp."""
    assert _resume_since({"input": {}}) is None
    assert _resume_since({"input": {"resume_since": "2026-09-12T12:34:56Z"}}) == datetime(
        2026, 9, 12, 12, 34, 56, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="timezone"):
        _resume_since({"input": {"resume_since": "2026-09-12T12:34:56"}})
    with pytest.raises(ValueError, match="ISO 8601"):
        _resume_since({"input": {"resume_since": "yesterday"}})


def test_reusable_reports_require_verified_immutable_bytes(tmp_path, monkeypatch):
    """Only a recent document whose stored body matches its digest can skip a download."""
    body = b"%PDF-verified-body"
    digest = save_bytes(tmp_path, body)
    fetched_at = datetime(2026, 9, 12, 12, 45, tzinfo=UTC)
    rows = [
        {
            "report_source_id": 4,
            "company_cik": "0000000001",
            "source_url": "https://example.net/report.pdf",
            "title": "Example Sustainability Report",
            "categories": ["environment_report"],
            "reporting_period": "FY2025",
            "document_id": 8,
            "document_url": "https://example.net/report.pdf",
            "final_url": "https://cdn.example.com/report.pdf",
            "sha256": digest,
            "byte_count": len(body),
            "fetched_at": fetched_at,
        },
        {
            "report_source_id": 5,
            "company_cik": "0000000002",
            "source_url": "https://example.org/missing.pdf",
            "title": "Missing report",
            "categories": ["social_employee"],
            "reporting_period": None,
            "document_id": 9,
            "document_url": "https://example.org/missing.pdf",
            "final_url": "https://example.org/missing.pdf",
            "sha256": "0" * 64,
            "byte_count": 10,
            "fetched_at": fetched_at,
        },
    ]

    class Result:
        def fetchall(self):
            return rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args):
            return Result()

    monkeypatch.setattr(sustainability_reports.db, "connect", lambda settings: Connection())
    reusable = _load_reusable_reports(
        SimpleNamespace(data_dir=tmp_path),
        [{"cik": "0000000001"}, {"cik": "0000000002"}],
        datetime(2026, 9, 12, 12, tzinfo=UTC),
    )
    assert set(reusable) == {"0000000001"}
    assert reusable["0000000001"]["https://example.net/report.pdf"] == reusable[
        "0000000001"
    ]["https://cdn.example.com/report.pdf"]
    assert reusable["0000000001"]["https://example.net/report.pdf"][
        "categories"
    ] == ["environment_report"]


def test_resume_skips_only_matching_pdf_request_but_keeps_page_discovery():
    """A rediscovered verified URL is counted as reused without a PDF network request."""
    company = {"cik": "0000000001", "name": "Example", "symbols": ["EXM"]}
    receipt = {
        "report_source_id": 4,
        "document_id": 8,
        "source_url": "https://example.net/example-2026.pdf",
        "final_url": "https://example.net/example-2026.pdf",
        "title": "Example Sustainability Report",
        "categories": ["environment_report", "social_employee"],
        "reporting_period": "FY2025",
        "sha256": "a" * 64,
        "byte_count": 100,
        "fetched_at": "2026-09-12T12:45:00+00:00",
    }
    spider = SustainabilityReportsSpider(
        task={"id": 123, "input": {"resume_since": "2026-09-12T12:00:00Z"}},
        settings_value=SimpleNamespace(),
        companies=[company],
        reusable_reports={
            company["cik"]: {"https://example.net/example-2026.pdf": receipt}
        },
    )
    spider.states[company["cik"]]["profile"] = {
        "profile_url": "https://www.responsibilityreports.com/Company/example",
        "company_name": "Example",
        "ticker": "EXM",
        "official_url": "https://www.example.com",
    }
    request = Request(
        "https://www.example.com/sustainability/reports",
        meta={"company_cik": company["cik"]},
    )
    response = HtmlResponse(
        request.url,
        request=request,
        body=b"""
        <title>Example 2026 Sustainability Reports</title>
        <a href="https://example.net/example-2026.pdf">View PDF</a>
        """,
        encoding="utf-8",
    )
    assert list(spider.parse_issuer_page(response)) == []
    state = spider.states[company["cik"]]
    assert state["visited_html"] == {request.url}
    assert state["reused_document_ids"] == {8}
    assert state["reused_reports"] == [receipt]
    assert state["status"] == "recent_report_reused"


def test_sec_website_seeds_bypass_directory_and_mark_unseeded_companies():
    """An explicit seed list starts issuer sites directly and never requests the directory."""
    companies = [
        {"cik": "0000000001", "name": "Example", "symbols": ["EXM"]},
        {"cik": "0000000002", "name": "Unseeded", "symbols": ["NONE"]},
    ]
    task = {
        "id": 126,
        "input": {
            "issuer_websites": [
                {
                    "company_cik": "0000000001",
                    "url": "https://www.example.com",
                    "source_document_id": 44,
                    "evidence_context": "Registrant website in SEC filing",
                    "confidence": "verified",
                }
            ]
        },
    }
    seed_mode, seeds = issuer_website_seeds(task, companies)
    assert seed_mode is True
    assert seeds["0000000001"]["identity_provider"] == "SEC filing"
    assert seeds["0000000001"]["profile_url"] == "document:44"
    spider = SustainabilityReportsSpider(
        task=task,
        settings_value=SimpleNamespace(),
        companies=companies,
        seed_mode=seed_mode,
        website_seeds=seeds,
    )

    async def collect_start_requests():
        return [request async for request in spider.start()]

    requests = asyncio.run(collect_start_requests())
    assert [request.url.rstrip("/") for request in requests] == ["https://www.example.com"]
    assert all("responsibilityreports.com" not in request.url for request in requests)
    assert (
        spider.states["0000000002"]["status"]
        == "missing_official_website_seed"
    )


def test_sec_seed_provenance_reaches_saved_report_source():
    """SEC identity evidence is preserved without a ResponsibilityReports provider claim."""
    source = report_source_from_item(
        {
            "source_url": "https://example.com/report.pdf",
            "title": "Example Sustainability Report",
            "identity_provider": "SEC filing",
            "profile_url": "document:44",
            "provider_company_name": "Example",
            "provider_ticker": "EXM",
            "official_website": "https://www.example.com/",
            "source_document_id": 44,
            "identity_evidence_context": "Registrant website in SEC filing",
            "identity_confidence": "verified",
            "identity_discovered_via": "issuer_site_via_sec_filing",
            "discovered_on": "https://www.example.com/sustainability",
        },
        {
            "categories": ["environment_report"],
            "reporting_period": "FY2025",
            "evidence": [
                {"category": "environment_report", "page": 2, "text": "Scope 1"}
            ],
        },
    )
    assert source["discovered_via"] == "issuer_site_via_sec_filing"
    assert source["evidence"][0]["provider"] == "SEC filing"
    assert source["evidence"][0]["source_document_id"] == 44
    assert "ResponsibilityReports.com" not in json.dumps(source)
