"""Pure checks for Q4 ContentAsset discovery and crawler integration."""

import json
from types import SimpleNamespace

import pytest
from scrapy import Request
from scrapy.http import HtmlResponse, Response

from green500.q4_sources import (
    parse_q4_content_asset_response,
    q4_content_asset_widgets,
)
from green500.sustainability_reports import (
    SustainabilityReportsSpider,
    classify_report_pages,
    issuer_website_seeds,
    report_source_from_item,
)

AIRBNB_WIDGET_HTML = """
<html><head>
<script src="https://widgets.q4app.com/widgets/q4.api.1.13.5.min.js"></script>
</head><body>
<div class="module-downloads-esg"></div>
<script>
$('.module-downloads-esg').downloads({
  showAllYears: true,
  downloadType: 'ESG',
  minYear: 2025,
  fetchAllYears: true
});
</script>
<a href="https://s26.q4cdn.com/656283129/files/Airbnb-2024-Sustainability.pdf">
  2024 Sustainability Update PDF
</a>
</body></html>
"""


def _airbnb_rows():
    """Retain the ordering fields from the saved six-row Airbnb response."""
    return {
        "GetContentAssetListResult": [
            {
                "ContentAssetId": 236,
                "FilePath": "https://s26.q4cdn.com/656283129/files/doc_governance/2025/dec/Airbnb-2025-Sustainability-Update.pdf",
                "FileSize": "23.56 MB",
                "FileType": "PDF",
                "Title": "2025 Update",
                "Type": "ESG",
                "RevisionNumber": 38043,
                "WorkflowId": "86e9b87e-2444-4a46-ad8c-9a75953bd1cd",
                "ContentAssetDate": "12/09/2025 00:00:00",
            },
            {
                "ContentAssetId": 239,
                "FilePath": "https://s26.q4cdn.com/656283129/files/doc_governance/2025/dec/Airbnb-AB-1305-Disclosure-FY2024-Posted-Dec-2025.pdf",
                "FileSize": "70 KB",
                "FileType": "PDF",
                "Title": "California AB 1305 Statement",
                "Type": "ESG",
                "RevisionNumber": 38060,
                "WorkflowId": "355ed219-7ac0-4fea-ba41-43e80d6a4877",
                "ContentAssetDate": "11/28/2025 00:00:00",
            },
            {
                "ContentAssetId": 222,
                "FilePath": "https://s26.q4cdn.com/656283129/files/doc_downloads/governance_doc_updated/2025/03/Airbnb-2024-Sustainability-and-Community-Update.pdf",
                "FileSize": "10.94 MB",
                "FileType": "PDF",
                "Title": "2024 Update",
                "Type": "ESG",
                "RevisionNumber": 36668,
                "WorkflowId": "fd1a90a5-76b5-474d-aad7-14a33ec40fdc",
                "ContentAssetDate": "12/20/2024 00:00:00",
            },
            {
                "ContentAssetId": 204,
                "FilePath": "https://s26.q4cdn.com/656283129/files/doc_downloads/governance_doc_updated/2023/Airbnb-SustainabilityandCommunityUpdate-2023-111623.pdf",
                "FileSize": "13.21 MB",
                "FileType": "PDF",
                "Title": "2023 Update",
                "Type": "ESG",
                "RevisionNumber": 35102,
                "WorkflowId": "f2a4aa8d-3f05-4a5f-bba3-02e7ff6af711",
                "ContentAssetDate": "11/09/2023 00:00:00",
            },
        ]
    }


def _response(url, body, *, content_type="text/html", meta=None):
    request = Request(url, meta=meta or {})
    return HtmlResponse(
        url,
        body=body.encode() if isinstance(body, str) else body,
        encoding="utf-8",
        headers={"Content-Type": content_type},
        request=request,
    )


def _spider():
    company = {"cik": "0001559720", "name": "Airbnb", "symbols": ["ABNB"]}
    spider = SustainabilityReportsSpider(
        task={"id": 1, "input": {}},
        settings_value=SimpleNamespace(),
        companies=[company],
    )
    spider.states[company["cik"]]["profile"] = {
        "ticker": "ABNB",
        "company_name": "Airbnb",
        "official_url": "https://investors.airbnb.com/governance/default.aspx",
        "profile_url": "document:1257",
        "identity_provider": "Reviewed official issuer hub",
        "discovered_via": "issuer_site_via_reviewed_official_hub",
        "source_document_id": 1257,
        "evidence_context": "Issuer governance hub reviewed directly.",
        "confidence": "high",
    }
    return spider, company


def test_q4_widget_detection_requires_q4_script_and_observed_asset_type():
    """A generic downloads call cannot make a non-Q4 page an API target."""
    page_url = "https://investors.airbnb.com/governance/default.aspx"
    widgets = q4_content_asset_widgets(AIRBNB_WIDGET_HTML, page_url)
    assert len(widgets) == 1
    assert widgets[0]["asset_type"] == "ESG"
    assert widgets[0]["fetch_all_years"] is True
    assert widgets[0]["min_year"] == 2025
    assert widgets[0]["api_url"] == (
        "https://investors.airbnb.com/feed/ContentAsset.svc/GetContentAssetList?"
        "assetType=ESG&pageSize=-1&pageNumber=0&tagList=&includeTags=true&year=-1&"
        "excludeSelection=1&LanguageId=1"
    )
    assert (
        q4_content_asset_widgets(
            "<script>$('.x').downloads({downloadType:'ESG'})</script>", page_url
        )
        == []
    )


def test_saved_airbnb_rows_rank_updates_before_ab1305_and_preserve_listing_date():
    """The saved provider rows rank report editions within family before legal updates."""
    widget = q4_content_asset_widgets(
        AIRBNB_WIDGET_HTML, "https://investors.airbnb.com/governance/default.aspx"
    )[0]
    candidates, issues = parse_q4_content_asset_response(
        json.dumps(_airbnb_rows()).encode(), "application/json; charset=utf-8", widget
    )
    assert issues == []
    assert [item["title"] for item in candidates] == [
        "2025 Update",
        "2024 Update",
        "2023 Update",
        "California AB 1305 Statement",
    ]
    assert candidates[0]["publisher_asset_date"] == "2025-12-09"
    assert candidates[0]["reporting_period"] is None
    assert candidates[0]["year"] == 2025
    assert candidates[0]["discovery_evidence"]["content_asset_id"] == 236
    assert candidates[0]["discovery_evidence"]["reporting_period"] is None


def test_invalid_q4_rows_are_skipped_and_html_challenge_is_rejected():
    widget = q4_content_asset_widgets(
        AIRBNB_WIDGET_HTML, "https://investors.airbnb.com/governance/default.aspx"
    )[0]
    value = {
        "GetContentAssetListResult": [
            {
                "Type": "ESG",
                "Title": "Bad date",
                "FilePath": "https://example.com/report.pdf",
                "ContentAssetDate": "yesterday",
            },
            {
                "Type": "ESG",
                "Title": "Local URL",
                "FilePath": "http://127.0.0.1/report.pdf",
                "ContentAssetDate": "12/09/2025 00:00:00",
            },
        ]
    }
    candidates, issues = parse_q4_content_asset_response(
        json.dumps(value).encode(), "application/json", widget
    )
    assert candidates == []
    assert issues == [
        "row 0: invalid ContentAssetDate",
        "row 1: invalid public PDF record",
    ]
    with pytest.raises(ValueError, match="not JSON"):
        parse_q4_content_asset_response(b"<html>challenge</html>", "text/html", widget)


def test_q4_page_defers_static_pdf_until_ranked_api_response():
    spider, company = _spider()
    page = _response(
        "https://investors.airbnb.com/governance/default.aspx",
        AIRBNB_WIDGET_HTML,
        meta={"company_cik": company["cik"], "purpose": "issuer_html"},
    )
    requests = list(spider.parse_issuer_page(page))
    api_request = next(
        item for item in requests if item.meta["purpose"] == "q4_content_asset_api"
    )
    assert not any(item.meta["purpose"] == "issuer_pdf" for item in requests)
    api_response = Response(
        api_request.url,
        body=json.dumps(_airbnb_rows()).encode(),
        headers={"Content-Type": "application/json"},
        request=api_request,
    )
    pdf_requests = list(
        spider.parse_q4_content_assets(api_response, **api_request.cb_kwargs)
    )
    assert [item.url for item in pdf_requests] == [
        (
            "https://s26.q4cdn.com/656283129/files/doc_governance/2025/dec/"
            "Airbnb-2025-Sustainability-Update.pdf"
        )
    ]
    pdf_response = Response(
        pdf_requests[0].url,
        body=b"%PDF-body-placeholder",
        headers={"Content-Type": "application/pdf"},
        request=pdf_requests[0],
    )
    item = next(
        spider.parse_report(pdf_response, pdf_requests[0].cb_kwargs["candidate"])
    )
    assert item["title"] == "2025 Update"
    assert item["topic_hints"] == ["environment", "social"]
    classified = classify_report_pages(
        [
            (1, "Fiscal Year 2025 Scope 1 emissions and energy performance."),
            (2, "Employees completed workplace health and safety training."),
        ],
        " ".join([item["title"], *item["topic_hints"]]),
    )
    assert classified["categories"] == [
        "environment_report",
        "social_employee",
    ]


def test_invalid_q4_response_falls_back_with_visible_api_error():
    spider, company = _spider()
    page = _response(
        "https://investors.airbnb.com/governance/default.aspx",
        AIRBNB_WIDGET_HTML,
        meta={"company_cik": company["cik"], "purpose": "issuer_html"},
    )
    api_request = next(
        item
        for item in spider.parse_issuer_page(page)
        if item.meta["purpose"] == "q4_content_asset_api"
    )
    challenge = Response(
        api_request.url,
        body=b"<html>challenge</html>",
        headers={"Content-Type": "text/html"},
        request=api_request,
    )
    fallback = list(spider.parse_q4_content_assets(challenge, **api_request.cb_kwargs))
    assert [item.url for item in fallback] == [
        "https://s26.q4cdn.com/656283129/files/Airbnb-2024-Sustainability.pdf"
    ]
    proof = fallback[0].cb_kwargs["candidate"]["discovery_evidence"]
    assert proof["category"] == "q4_content_asset_api_fallback"
    assert proof["api_error"] == "Q4 ContentAsset response is not JSON."
    assert spider.states[company["cik"]]["q4_api_failures"]


def test_q4_listing_proof_survives_verified_body_classification():
    proof = {"category": "q4_content_asset_listing", "content_asset_id": 236}
    source = report_source_from_item(
        {
            "identity_provider": "Reviewed official issuer hub",
            "profile_url": "document:1257",
            "provider_company_name": "Airbnb",
            "provider_ticker": "ABNB",
            "official_website": "https://investors.airbnb.com/governance/",
            "source_document_id": 1257,
            "identity_evidence_context": "Reviewed issuer hub",
            "identity_confidence": "high",
            "discovered_on": "https://investors.airbnb.com/governance/",
            "identity_discovered_via": "issuer_site_via_reviewed_official_hub",
            "source_url": "https://s26.q4cdn.com/report.pdf",
            "title": "2025 Update",
            "discovery_evidence": proof,
        },
        {
            "categories": ["environment_report"],
            "reporting_period": "FY2025",
            "evidence": [{"category": "environment_report", "page": 1}],
        },
    )
    assert source["evidence"][1] == proof


def test_explicit_reviewed_hub_seed_does_not_claim_sec_identity():
    company = {"cik": "0001559720", "name": "Airbnb", "symbols": ["ABNB"]}
    seed_mode, seeds = issuer_website_seeds(
        {
            "input": {
                "issuer_websites": [
                    {
                        "company_cik": company["cik"],
                        "url": "https://investors.airbnb.com/governance/",
                        "source_document_id": 1257,
                        "evidence_context": "Current official governance hub reviewed.",
                        "confidence": "high",
                        "identity_provider": "Reviewed official issuer hub",
                    }
                ]
            }
        },
        [company],
    )
    assert seed_mode is True
    assert seeds[company["cik"]]["identity_provider"] == (
        "Reviewed official issuer hub"
    )
    assert seeds[company["cik"]]["discovered_via"] == (
        "issuer_site_via_reviewed_official_hub"
    )
