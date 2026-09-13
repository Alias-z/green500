"""Closed environmental feature contract for extraction and ML export."""

from green500.processing.fixed_schema import build_model

FIELDS = {
    "env_total_ghg_tco2e": {"unit": "tCO2e", "type": "number", "description": "Total company greenhouse-gas footprint across the scopes included by the report."},
    "env_scope_1_tco2e": {"unit": "tCO2e", "type": "number", "description": "Gross Scope 1 greenhouse-gas emissions."},
    "env_scope_2_location_based_tco2e": {"unit": "tCO2e", "type": "number", "description": "Location-based Scope 2 greenhouse-gas emissions."},
    "env_scope_2_market_based_tco2e": {"unit": "tCO2e", "type": "number", "description": "Market-based Scope 2 greenhouse-gas emissions."},
    "env_scope_3_total_tco2e": {"unit": "tCO2e", "type": "number", "description": "Reported complete Scope 3 greenhouse-gas inventory."},
    "env_total_energy_mwh": {"unit": "MWh", "type": "number", "description": "Total energy consumed."},
    "env_renewable_electricity_percent": {"unit": "percent", "type": "number", "description": "Renewable electricity share from 0 to 100."},
    "env_water_withdrawal_m3": {"unit": "m3", "type": "number", "description": "Water withdrawal."},
    "env_total_waste_tonnes": {"unit": "t", "type": "number", "description": "Total waste generated in metric tonnes."},
    "env_waste_recycled_percent": {"unit": "percent", "type": "number", "description": "Reported share of waste recycled from 0 to 100."},
}

MODEL = build_model("FixedEnvironmentExtraction", FIELDS)


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
    """Return all fixed environmental predictors and their separate metadata."""
    if data is None:
        reason = "No fixed environmental extraction is available."
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
            metadata[field]["reason"] = "A company target is excluded from actual environmental predictors."
        if values[field] is not None and metadata[field]["reporting_year"] != result.reporting_year:
            values[field] = None
            metadata[field]["status"] = "conflicting"
            metadata[field]["reason"] = "The field does not use the extraction's common reporting year."
        if values[field] is not None and values[field] < 0:
            values[field] = None
            metadata[field]["status"] = "conflicting"
            metadata[field]["reason"] = "A physical environmental predictor cannot be negative."
        if FIELDS[field]["unit"] == "percent" and values[field] is not None and values[field] > 100:
            values[field] = None
            metadata[field]["status"] = "conflicting"
            metadata[field]["reason"] = "A percentage predictor must be between 0 and 100."
    return values, metadata
