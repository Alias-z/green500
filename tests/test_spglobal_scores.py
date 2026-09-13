"""Verify strict parsing and explicit company coverage for S&P public scores."""

import json
from datetime import UTC, datetime

import pytest

from green500.spglobal_scores import (
    apply_score_observations,
    build_company_coverage,
    match_official_candidates,
    parse_public_score_response,
    score_url,
    validate_approved_export,
)

PUBLIC_SCORE_TEXT = b"""Example Manufacturer ESG Score
The score is relative to peers within the same industry classification.
Company
Industry
CSA Score
ESG Score
Score Under Review
Last Updated
Example Manufacturer
Example Industry
12
34
NO
January 02, 2026
This company is a Corporate Sustainability Assessment (CSA) survey respondent.
"""


def test_public_score_parser_keeps_score_types_and_dates_separate():
    result = parse_public_score_response(
        PUBLIC_SCORE_TEXT,
        provider_cid="4004214",
        captured_url=score_url("4004214"),
        fetched_at=datetime(2026, 9, 12, 16, 22, tzinfo=UTC),
        content_type="text/plain",
    )

    assert result["company_name"] == "Example Manufacturer"
    assert result["public_score_status"] == "available"
    assert result["csa_score"] == 12
    assert result["esg_score"] == 34
    assert result["scores"] == [
        {"score_type": "csa_score", "value": 12},
        {"score_type": "esg_score", "value": 34},
    ]
    assert result["last_updated"] == "2026-01-02"
    assert result["assessment_year"] is None
    assert result["available_dimension_scores"] == []
    assert result["score_under_review"] is False
    assert result["is_csa_survey_respondent"] is True


def test_participation_uses_company_notice_not_generic_legend():
    generic_legend = PUBLIC_SCORE_TEXT.replace(
        b"This company is a Corporate Sustainability Assessment (CSA) survey respondent.\n",
        b"Non-participating companies - ESG Score is based on information available "
        b"in the public domain and modeling approaches and is not based on the "
        b"company's active participation in the Corporate Sustainability Assessment.\n",
    )
    result = parse_public_score_response(
        generic_legend,
        provider_cid="4004214",
        captured_url=score_url("4004214"),
        fetched_at=datetime(2026, 9, 12, 16, 22, tzinfo=UTC),
        content_type="text/plain",
    )
    assert result["is_csa_survey_respondent"] is None


def test_non_participation_uses_explicit_company_notice():
    company_notice = PUBLIC_SCORE_TEXT.replace(
        b"This company is a Corporate Sustainability Assessment (CSA) survey respondent.\n",
        b"This company's ESG Score is based on publicly available information and "
        b"modeling approaches and is not based on the company's active participation "
        b"in the Corporate Sustainability Assessment (CSA).\n",
    )
    result = parse_public_score_response(
        company_notice,
        provider_cid="4004214",
        captured_url=score_url("4004214"),
        fetched_at=datetime(2026, 9, 12, 16, 22, tzinfo=UTC),
        content_type="text/plain",
    )
    assert result["is_csa_survey_respondent"] is False


def test_duplicate_provider_blocks_must_agree():
    conflicting = PUBLIC_SCORE_TEXT + PUBLIC_SCORE_TEXT.replace(b"\n34\n", b"\n35\n")
    with pytest.raises(ValueError, match="conflicting"):
        parse_public_score_response(
            conflicting,
            provider_cid="4004214",
            captured_url=score_url("4004214"),
            fetched_at=datetime(2026, 9, 12, 16, 22, tzinfo=UTC),
            content_type="text/plain",
        )


def test_zero_is_a_reported_score_and_never_treated_as_missing():
    zero_scores = PUBLIC_SCORE_TEXT.replace(b"\n12\n34\n", b"\n0\n0\n")
    result = parse_public_score_response(
        zero_scores,
        provider_cid="12",
        captured_url=score_url("12"),
        fetched_at=datetime(2026, 9, 12, 16, 22, tzinfo=UTC),
        content_type="text/plain",
    )
    assert result["public_score_status"] == "available"
    assert result["csa_score"] == 0
    assert result["esg_score"] == 0


def test_premium_only_page_preserves_identity_and_null_scores():
    body = b"""Company
Industry
CSA Score
ESG Score
Score Under Review
Last Updated
Example Manufacturer
Example Industry
This company's ESG Score and underlying data are available via our premium channels.
"""
    result = parse_public_score_response(
        body,
        provider_cid="12",
        captured_url=score_url("12"),
        fetched_at=datetime(2026, 9, 12, 16, 22, tzinfo=UTC),
        content_type="text/plain",
    )
    assert result["public_score_status"] == "premium_only"
    assert result["company_name"] == "Example Manufacturer"
    assert result["csa_score"] is None
    assert result["esg_score"] is None
    assert result["last_updated"] is None
    assert result["is_csa_survey_respondent"] is None


def test_provider_cid_is_never_derived_from_company_name():
    companies = [
        {"cik": "0000789019", "name": "Microsoft Corp.", "symbols": ["MSFT"]},
        {"cik": "0000320193", "name": "Apple Inc.", "symbols": ["AAPL"]},
    ]
    coverage = build_company_coverage(
        companies,
        {
            "0000789019": {
                "provider_cid": "4004214",
                "company_name": "Microsoft Corporation",
                "mapping_evidence_url": score_url("4004214"),
            }
        },
    )

    assert coverage[0]["identity_status"] == "matched"
    assert coverage[0]["provider_cid"] == "4004214"
    assert coverage[0]["outcome"] == "not_checked"
    assert coverage[0]["access_status"] == "not_requested"
    assert coverage[0]["score_status"] == "not_checked"
    assert coverage[0]["csa_score"] is None
    assert coverage[0]["esg_score"] is None
    assert coverage[0]["assessment_year"] is None
    assert coverage[1]["identity_status"] == "not_checked"
    assert coverage[1]["outcome"] == "not_checked"
    assert coverage[1]["provider_cid"] is None


def test_provider_cid_cannot_be_reused_across_companies():
    companies = [
        {"cik": "0000789019", "name": "Company one", "symbols": ["ONE"]},
        {"cik": "0000320193", "name": "Company two", "symbols": ["TWO"]},
    ]
    mappings = {
        cik: {
            "provider_cid": "4004214",
            "company_name": f"Provider {cik}",
            "mapping_evidence_url": score_url("4004214"),
        }
        for cik in ("0000789019", "0000320193")
    }
    with pytest.raises(ValueError, match="reuses a provider CID"):
        build_company_coverage(companies, mappings)


def test_candidate_match_preserves_business_boundary_and_requires_unique_cid():
    company = {"normalized_issuer_names": ["example holdings"]}
    candidates = [
        {
            "provider_cid": "12",
            "provider_company_name": "Example Holdings Corporation",
        },
        {"provider_cid": "13", "provider_company_name": "Example Corporation"},
    ]
    assert match_official_candidates(company, candidates) == {
        "status": "matched",
        "match_method": "exact_normalized_issuer_name",
        "provider_cid": "12",
        "provider_company_name": "Example Holdings Corporation",
    }
    candidates.append(
        {"provider_cid": "14", "provider_company_name": "Example Holdings Inc."}
    )
    assert match_official_candidates(company, candidates)["status"] == "ambiguous"

    assert (
        match_official_candidates(
            {"normalized_issuer_names": ["aes"]},
            [{"provider_cid": "15", "provider_company_name": "The AES Corporation"}],
        )["status"]
        == "matched"
    )


def test_mapping_evidence_url_must_contain_the_same_cid():
    companies = [{"cik": "0000789019", "name": "Microsoft", "symbols": ["MSFT"]}]
    with pytest.raises(ValueError, match="requested CID"):
        build_company_coverage(
            companies,
            {
                "0000789019": {
                    "provider_cid": "4004214",
                    "company_name": "Microsoft Corporation",
                    "mapping_evidence_url": score_url("4004205"),
                }
            },
        )


def test_observation_requires_matching_cid_url_and_provider_company_name():
    coverage = build_company_coverage(
        [{"cik": "0000789019", "name": "Microsoft", "symbols": ["MSFT"]}],
        {
            "0000789019": {
                "provider_cid": "4004214",
                "company_name": "Microsoft Corporation",
                "mapping_evidence_url": score_url("4004214"),
            }
        },
    )
    provider_text = PUBLIC_SCORE_TEXT.replace(
        b"Example Manufacturer", b"Microsoft Corporation"
    )
    observation = parse_public_score_response(
        provider_text,
        provider_cid="4004214",
        captured_url=score_url("4004214"),
        fetched_at=datetime(2026, 9, 12, 16, 22, tzinfo=UTC),
        content_type="text/plain",
    )
    merged = apply_score_observations(coverage, [observation])
    assert merged[0]["outcome"] == "matched"
    assert merged[0]["access_status"] == "succeeded"
    assert merged[0]["source_sha256"] == observation["source_sha256"]

    observation["provider_url"] = score_url("4004205")
    with pytest.raises(ValueError, match="URL"):
        apply_score_observations(coverage, [observation])


@pytest.mark.parametrize("provider_cid", ["", "Microsoft", "000012", "0", "1" * 20])
def test_invalid_provider_cids_are_rejected(provider_cid):
    with pytest.raises(ValueError, match="CID"):
        score_url(provider_cid)


def test_captured_url_must_match_requested_cid():
    with pytest.raises(ValueError, match="requested CID"):
        parse_public_score_response(
            PUBLIC_SCORE_TEXT,
            provider_cid="4004214",
            captured_url=score_url("4004205"),
            fetched_at=datetime(2026, 9, 12, 16, 22, tzinfo=UTC),
            content_type="text/plain",
        )


def licensed_export(**row_changes):
    row = {
        "company_cik": "0000789019",
        "provider_cid": "4004214",
        "provider_company_name": "Microsoft Corporation",
        "industry": "SOF Software",
        "score_type": "esg_score",
        "score_value": 42,
        "score_under_review": False,
        "last_updated": "2026-09-09",
        "assessment_year": None,
    }
    row.update(row_changes)
    return json.dumps(
        {
            "provider": "S&P Global Sustainable1",
            "license_reference": "provider-agreement-reference",
            "fetched_at": "2026-09-12T16:30:00+00:00",
            "rows": [row],
        }
    ).encode()


def test_approved_export_requires_verified_identity_and_keeps_year_null():
    result = validate_approved_export(
        licensed_export(),
        {
            "0000789019": {
                "provider_cid": "4004214",
                "company_name": "Microsoft Corporation",
            }
        },
    )
    assert result["rows"][0]["assessment_year"] is None
    assert result["rows"][0]["score_type"] == "esg_score"
    assert result["rows"][0]["provider_url"] == score_url("4004214")


@pytest.mark.parametrize(
    ("row_changes", "message"),
    [
        ({"provider_cid": "4004205"}, "verified company identity"),
        ({"score_value": 101}, "score value"),
        ({"score_value": None}, "score value"),
        ({"assessment_year": "2026"}, "assessment year"),
        ({"score_type": "combined_score"}, "score type"),
    ],
)
def test_approved_export_rejects_unverified_or_malformed_scores(row_changes, message):
    with pytest.raises(ValueError, match=message):
        validate_approved_export(
            licensed_export(**row_changes),
            {
                "0000789019": {
                    "provider_cid": "4004214",
                    "company_name": "Microsoft Corporation",
                }
            },
        )
