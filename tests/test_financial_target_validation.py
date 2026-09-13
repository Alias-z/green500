from green500.processing.financial_target_features import FIELDS
from green500.processing.financial_target_validation import (
    financial_target_source_hints,
    normalize_financial_target_result,
)


def _metadata(quote, raw_value="45"):
    return {
        "status": "company_target",
        "reporting_year": 2025,
        "reason": None,
        "qualification": None,
        "confidence": 1.0,
        "evidence": {
            "block_id": 1,
            "quote": quote,
            "raw_value": raw_value,
            "source_unit": "percent",
            "scale_factor": 1.0,
        },
    }


def _result():
    return {
        "company": {"name": "Example", "ticker": "EX"},
        "reporting_year": 2025,
        "boundary": "Consolidated",
        "limitations": [],
        "values": dict.fromkeys(FIELDS),
        "metadata": dict.fromkeys(FIELDS),
    }


def test_contract_has_five_interval_slots_and_ten_fields():
    assert len(FIELDS) == 10
    assert all(field.endswith(("_min", "_max")) for field in FIELDS)


def test_keeps_lower_bound_and_duplicates_point_endpoint():
    result = _result()
    lower = "financial_target_operating_margin_long_term_min"
    result["values"][lower] = 45.0
    result["metadata"][lower] = _metadata("Premium operating margin at ≥ 45%")
    point = "financial_target_revenue_growth_annual_min"
    result["values"][point] = 7.0
    result["metadata"][point] = _metadata("Full-year revenue growth target 7%", "7")
    evidence = {
        "blocks": [
            {
                "block_id": 1,
                "locator": {},
                "text": "Premium operating margin at ≥ 45%. Full-year revenue growth target 7%.",
            }
        ]
    }

    normalized, audit = normalize_financial_target_result(result, evidence)

    assert normalized["values"][lower] == 45.0
    assert normalized["values"]["financial_target_operating_margin_long_term_max"] is None
    assert normalized["values"][point] == 7.0
    assert normalized["values"]["financial_target_revenue_growth_annual_max"] == 7.0
    assert any(item["action"] == "duplicated_point_endpoint" for item in audit)


def test_rejects_margin_expansion_as_margin_level():
    result = _result()
    field = "financial_target_operating_margin_annual_min"
    result["values"][field] = 0.7
    result["metadata"][field] = _metadata("Operating margin expansion of 70 bps", "70")
    evidence = {
        "blocks": [
            {
                "block_id": 1,
                "locator": {},
                "text": "Operating margin expansion of 70 bps",
            }
        ]
    }

    normalized, _ = normalize_financial_target_result(result, evidence)

    assert normalized["values"][field] is None


def test_source_hints_keep_only_supported_target_types():
    evidence = {
        "blocks": [
            {
                "block_id": 7,
                "locator": {"page": 3},
                "text": "Full-year 2026 guidance\nOperating margin 44% to 45%",
            },
            {
                "block_id": 8,
                "locator": {"page": 4},
                "text": "Free cash flow target USD 4 billion",
            },
        ]
    }

    hints = financial_target_source_hints(evidence, max_count=4, max_chars=2_000)

    assert len(hints) == 1
    assert hints[0]["block_id"] == 7
    assert hints[0]["candidate_target_types"] == ["operating_margin"]
    assert hints[0]["raw_value_tokens"] == ["44%", "45%"]
