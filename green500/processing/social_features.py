"""Closed social feature contract for extraction and ML export."""

from green500.processing.fixed_schema import build_model

FIELDS = {
    "social_employees_count": {"unit": "count", "type": "integer", "description": "Employees within the stated company boundary."},
    "social_employee_turnover_percent": {"unit": "percent", "type": "number", "description": "Employee turnover from 0 to 100."},
    "social_women_workforce_percent": {"unit": "percent", "type": "number", "description": "Women as a share of the workforce from 0 to 100."},
    "social_employee_fatalities_count": {"unit": "count", "type": "integer", "description": "Employee fatalities."},
    "social_contractor_fatalities_count": {"unit": "count", "type": "integer", "description": "Contractor fatalities."},
    "social_recordable_injury_rate_per_200000_hours": {"unit": "rate per 200000 hours", "type": "number", "description": "Recordable injuries per 200,000 hours worked."},
    "social_recordable_injury_rate_per_1000000_hours": {"unit": "rate per 1000000 hours", "type": "number", "description": "Recordable injuries per 1,000,000 hours worked."},
    "social_suppliers_audited_count": {"unit": "count", "type": "integer", "description": "Suppliers audited during the reporting period."},
    "social_confirmed_violations_count": {"unit": "count", "type": "integer", "description": "Confirmed human-rights or supply-chain violations."},
    "social_community_investment_usd": {"unit": "USD", "type": "number", "description": "Community investment explicitly reported in US dollars."},
}

MODEL = build_model("FixedSocialExtraction", FIELDS)


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
    """Return all fixed social predictors and their separate metadata."""
    if data is None:
        reason = "No fixed social extraction is available."
        return (
            {field: None for field in FIELDS},
            {field: _missing_metadata(reason) for field in FIELDS},
        )
    result = MODEL.model_validate(data)
    values = result.values.model_dump(mode="json")
    metadata = result.metadata.model_dump(mode="json")
    for field in FIELDS:
        if metadata[field] is None:
            metadata[field] = _missing_metadata("The extraction supplied no field metadata.")
        if values[field] is not None and metadata[field]["status"] == "company_target":
            values[field] = None
            metadata[field]["status"] = "not_applicable"
            metadata[field]["reason"] = "A company target is excluded from actual social predictors."
        if values[field] is not None and metadata[field]["reporting_year"] != result.reporting_year:
            values[field] = None
            metadata[field]["status"] = "conflicting"
            metadata[field]["reason"] = "The field does not use the extraction's common reporting year."
        if values[field] is not None and values[field] < 0:
            values[field] = None
            metadata[field]["status"] = "conflicting"
            metadata[field]["reason"] = "A social predictor cannot be negative."
        if FIELDS[field]["unit"] == "percent" and values[field] is not None and values[field] > 100:
            values[field] = None
            metadata[field]["status"] = "conflicting"
            metadata[field]["reason"] = "A percentage predictor must be between 0 and 100."
    return values, metadata
