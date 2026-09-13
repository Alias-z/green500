"""Ten common financial-target predictors for comparable ML input."""

FIELDS = {
    "financial_target_revenue_growth_annual_min": {"unit": "percent", "type": "number", "description": "Lower endpoint of consolidated annual revenue-growth guidance."},
    "financial_target_revenue_growth_annual_max": {"unit": "percent", "type": "number", "description": "Upper endpoint of consolidated annual revenue-growth guidance."},
    "financial_target_operating_margin_annual_min": {"unit": "percent", "type": "number", "description": "Lower endpoint of consolidated annual operating-margin guidance."},
    "financial_target_operating_margin_annual_max": {"unit": "percent", "type": "number", "description": "Upper endpoint of consolidated annual operating-margin guidance."},
    "financial_target_revenue_growth_long_term_min": {"unit": "percent", "type": "number", "description": "Lower endpoint of consolidated long-term revenue-growth target."},
    "financial_target_revenue_growth_long_term_max": {"unit": "percent", "type": "number", "description": "Upper endpoint of consolidated long-term revenue-growth target."},
    "financial_target_operating_margin_long_term_min": {"unit": "percent", "type": "number", "description": "Lower endpoint of consolidated long-term operating-margin target."},
    "financial_target_operating_margin_long_term_max": {"unit": "percent", "type": "number", "description": "Upper endpoint of consolidated long-term operating-margin target."},
    "financial_target_eps_growth_long_term_min": {"unit": "percent", "type": "number", "description": "Lower endpoint of consolidated long-term earnings-per-share growth target."},
    "financial_target_eps_growth_long_term_max": {"unit": "percent", "type": "number", "description": "Upper endpoint of consolidated long-term earnings-per-share growth target."},
}


def _missing_metadata(reason):
    return {
        "status": "not_extracted",
        "reporting_year": None,
        "reason": reason,
        "qualification": None,
        "confidence": None,
        "evidence": None,
    }


def project(data: dict | None) -> tuple[dict, dict]:
    """Validate and return the ten fixed financial-target endpoints."""
    values = dict.fromkeys(FIELDS)
    if data is None:
        reason = "No fixed financial-target extraction is available."
        return values, {field: _missing_metadata(reason) for field in FIELDS}
    from green500.processing.fixed_financial_targets_profile import MODEL

    parsed = MODEL.model_validate(data).model_dump(mode="json")
    values.update(parsed["values"])
    metadata = {
        field: parsed["metadata"][field]
        if parsed["metadata"][field] is not None
        else _missing_metadata("The extraction supplied no metadata for this null field.")
        for field in FIELDS
    }
    return values, metadata
