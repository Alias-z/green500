"""Verify sparse-feature readiness rules without production data or model calls."""

import json
from datetime import date

from green500.ml.audit import (
    PRIMITIVE_FEATURES_BY_CATEGORY,
    _companies_with_all_five_category_values,
    _null_status,
    _provisional_feature_eligibility,
    _repair_manifest_summary,
)
from green500.ml.feature_registry import predictive_feature_names


def selection_config():
    """Return small synthetic thresholds with the production contract shape."""
    return {
        "minimum_observed_companies": 5,
        "minimum_observed_fraction": 0.10,
        "minimum_continuous_distinct_values": 5,
        "minimum_boolean_count_per_level": 5,
        "maximum_ambiguous_missing_fraction": 0.10,
        "ambiguous_missing_statuses": [
            "not_extracted",
            "processing",
            "unknown",
            "unspecified_null",
        ],
        "core_categories": [
            "financial",
            "social",
            "environmental",
            "fixed_context",
        ],
        "optional_categories": ["climate_target", "financial_target"],
        "use_one_standard_error_rule": True,
    }


def test_null_statuses_keep_semantic_absence_separate_from_processing():
    """Semantic non-disclosure never becomes ambiguous extraction failure."""
    assert _null_status("completed", {"status": "not_disclosed"}) == "not_disclosed"
    assert _null_status("completed", {"status": "not_applicable"}) == "not_applicable"
    assert _null_status("processing", {"status": "not_disclosed"}) == "processing"
    assert _null_status("completed", {}) == "unspecified_null"


def test_provisional_eligibility_reports_support_variation_zeros_and_ambiguity():
    """Configured support checks report exclusions without deleting registry fields."""
    names = predictive_feature_names()
    raw_rows = [{name: None for name in names} for _ in range(20)]
    dated_rows = [{name: None for name in names} for _ in range(20)]
    missing_statuses = {name: ["not_disclosed"] * 20 for name in names}
    for index, (raw_row, dated_row) in enumerate(zip(raw_rows, dated_rows, strict=True)):
        raw_row["industry"] = dated_row["industry"] = "Industry " + str(index % 2)
        raw_row["financial_revenue_usd"] = index
        dated_row["financial_revenue_usd"] = index
        raw_row["climate_sbti_validated"] = index % 2 == 0
        dated_row["climate_sbti_validated"] = index % 2 == 0
        raw_row["social_employee_turnover_percent"] = index
        dated_row["social_employee_turnover_percent"] = index
    missing_statuses["industry"] = ["observed"] * 20
    missing_statuses["financial_revenue_usd"] = ["observed"] * 20
    missing_statuses["climate_sbti_validated"] = ["observed"] * 20
    missing_statuses["social_employee_turnover_percent"] = [
        "unknown",
        "unknown",
        "unknown",
        *(["observed"] * 17),
    ]

    readiness = _provisional_feature_eligibility(
        raw_rows, dated_rows, missing_statuses, selection_config()
    )

    revenue = readiness["financial_revenue_usd"]
    assert revenue["is_provisionally_eligible"] is True
    assert revenue["raw_support_count"] == 20
    assert revenue["dated_eligible_distinct_count"] == 20
    assert revenue["dated_eligible_zero_count"] == 1
    assert readiness["climate_sbti_validated"]["boolean_level_counts"] == {
        "false": 10,
        "true": 10,
    }
    assert readiness["social_employee_turnover_percent"][
        "provisional_exclusion_reasons"
    ] == ["ambiguous_missing_fraction_above_maximum"]


def test_all_five_category_count_uses_current_values_and_preserves_zero():
    """Five-category coverage is computed from raw primitives on every audit run."""
    complete = {}
    incomplete = {}
    for feature_names in PRIMITIVE_FEATURES_BY_CATEGORY.values():
        complete[feature_names[0]] = 0
        incomplete[feature_names[0]] = 1
    incomplete[PRIMITIVE_FEATURES_BY_CATEGORY["social"][0]] = None

    assert _companies_with_all_five_category_values([complete, incomplete]) == 1
    assert _companies_with_all_five_category_values([incomplete]) == 0


def test_repair_manifest_writes_full_rows_and_returns_only_summary(tmp_path):
    """Audit stdout stays compact while the requested repair artifact keeps all rows."""
    repair_rows = [
        {
            "issue": "missing_publication_date",
            "source_document_id": 7,
            "company_cik": "0000000001",
            "ticker": "TEST",
            "company_name": "Synthetic Company",
            "category": "environmental",
            "field": "env_scope_1_tco2e",
            "current_status": "reported",
            "current_reason": None,
            "audit_cutoff": "2026-09-13",
        }
    ]
    output_path = tmp_path / "repairs.json"

    summary = _repair_manifest_summary(repair_rows, date(2026, 9, 13), output_path)

    assert summary == {
        "read_only": True,
        "row_count": 1,
        "issue_counts": {"missing_publication_date": 1},
        "output_path": str(output_path),
    }
    assert "rows" not in summary
    assert json.loads(output_path.read_text())["rows"] == repair_rows
