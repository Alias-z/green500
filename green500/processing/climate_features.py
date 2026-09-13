"""Define the fixed climate fields used for extraction and machine learning."""

import math
import re

from green500.processing.feature_units import convert
from green500.processing.fixed_schema import build_model

_SCALAR_FIELDS = {
    "climate_net_zero_target_year": {
        "unit": "year",
        "type": "integer",
        "description": "Explicit company-wide net-zero target year.",
    },
    "climate_sbti_validated": {
        "unit": None,
        "type": "boolean",
        "description": "Whether an applicable target is validated by the Science Based Targets initiative.",
    },
}

TARGET_FAMILIES = (
    ("scope_1_2_absolute_reduction", "scope_1_2", "absolute_reduction"),
    ("scope_3_absolute_reduction", "scope_3", "absolute_reduction"),
)

_TARGET_ATTRIBUTES = {
    "baseline_year": {
        "unit": "year",
        "type": "integer",
        "description": "Baseline year for the selected target.",
    },
    "target_year": {
        "unit": "year",
        "type": "integer",
        "description": "Completion year for the selected target.",
    },
    "reduction_pct": {
        "unit": "percent",
        "type": "number",
        "description": "Reduction required by the selected target.",
    },
    "progress_pct": {
        "unit": "percent",
        "type": "number",
        "description": "Reported progress toward the selected target.",
    },
}

FIELDS = dict(_SCALAR_FIELDS)
for _family_name, _scope, _target_type in TARGET_FAMILIES:
    for _attribute, _definition in _TARGET_ATTRIBUTES.items():
        FIELDS[f"climate_target_{_family_name}_{_attribute}"] = {
            **_definition,
            "description": (
                f"{_definition['description']} Target slot: "
                f"{_scope} {_target_type}."
            ),
        }

MODEL = build_model("FixedClimateExtraction", FIELDS)


def _missing_metadata(reason):
    return {
        "status": "not_extracted",
        "reporting_year": None,
        "reason": reason,
        "qualification": None,
        "confidence": None,
        "evidence": None,
    }


def _raw_number(raw_value):
    if not isinstance(raw_value, str) or re.fullmatch(
        r"-?\d[\d,]*(?:\.\d+)?", raw_value
    ) is None:
        return None
    return float(raw_value.replace(",", ""))


def project(data: dict | None) -> tuple[dict, dict]:
    """Return fixed primitive values and matching field-level metadata."""
    if data is None:
        reason = "No fixed climate extraction is available."
        return (
            dict.fromkeys(FIELDS),
            {field: _missing_metadata(reason) for field in FIELDS},
        )
    parsed = MODEL.model_validate(data)
    values = parsed.values.model_dump(mode="json")
    metadata = {
        field: (
            getattr(parsed.metadata, field).model_dump(mode="json")
            if getattr(parsed.metadata, field) is not None
            else _missing_metadata(
                "The extraction supplied no metadata for this null field."
            )
        )
        for field in FIELDS
    }
    for field, value in list(values.items()):
        if value is None or isinstance(value, bool):
            continue
        details = metadata[field]
        evidence = details["evidence"]
        raw_number = _raw_number(evidence["raw_value"])
        target_unit = FIELDS[field]["unit"]
        converted = convert(raw_number, evidence["source_unit"], target_unit)
        scaled = (
            raw_number * evidence["scale_factor"]
            if raw_number is not None and evidence["scale_factor"] is not None
            else None
        )
        agrees = (
            converted is not None
            and scaled is not None
            and math.isclose(float(value), converted, rel_tol=1e-9, abs_tol=1e-9)
            and math.isclose(float(value), scaled, rel_tol=1e-9, abs_tol=1e-9)
        )
        if FIELDS[field]["type"] == "integer":
            agrees = agrees and float(value).is_integer()
        if agrees:
            continue
        values[field] = None
        metadata[field] = {
            **details,
            "status": "conflicting",
            "reason": (
                "The extracted value does not match the deterministic conversion "
                "from its quoted raw value and source unit."
            ),
        }
    if set(values) != set(FIELDS) or set(metadata) != set(FIELDS):
        raise RuntimeError("Climate projection changed the fixed field contract.")
    return values, metadata
