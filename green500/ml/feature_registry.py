"""Build the fixed Green500 machine-learning feature registry."""

from __future__ import annotations

from copy import deepcopy

from green500.feature_catalog import feature_schema
from green500.ml.contracts import APPROVED_FEATURE_CATEGORIES, FeatureDefinition

FEATURE_REGISTRY_VERSION = "green500-ml-feature-registry-v1"

_CATEGORY_NAMES = {
    "financial": "financial",
    "social_employee": "social",
    "environment_report": "environmental",
    "climate_targets": "climate_target",
    "financial_targets": "financial_target",
}

_FINANCIAL_SOURCE_FIELDS = {
    "financial_revenue_usd": "revenue",
    "financial_employees_count": "employees",
    "financial_total_capex_usd": "total_capex",
    "financial_green_transition_capex_usd": "green_transition_capex",
    "financial_operating_income_usd": "operating_income",
    "financial_total_assets_usd": "total_assets",
    "financial_cash_and_equivalents_usd": "cash_and_equivalents",
    "financial_total_debt_usd": "total_debt",
    "financial_operating_cash_flow_usd": "operating_cash_flow",
}

_DERIVED_FEATURES = {
    "operating_margin": {
        "source_JSON_path": "$derived.operating_margin",
        "data_type": "number",
        "canonical_unit": "ratio",
        "category_or_context": "financial",
        "validation_rules": [
            "Divide financial_operating_income_usd by financial_revenue_usd.",
            "Require a positive revenue denominator and compatible reporting periods, units, and boundaries.",
        ],
        "derived_feature_dependencies": [
            "financial_operating_income_usd",
            "financial_revenue_usd",
        ],
    },
    "debt_assets": {
        "source_JSON_path": "$derived.debt_assets",
        "data_type": "number",
        "canonical_unit": "ratio",
        "category_or_context": "financial",
        "validation_rules": [
            "Divide financial_total_debt_usd by financial_total_assets_usd.",
            "Require a positive assets denominator and compatible reporting periods, units, and boundaries.",
        ],
        "derived_feature_dependencies": [
            "financial_total_debt_usd",
            "financial_total_assets_usd",
        ],
    },
    "capex_revenue": {
        "source_JSON_path": "$derived.capex_revenue",
        "data_type": "number",
        "canonical_unit": "ratio",
        "category_or_context": "financial",
        "validation_rules": [
            "Divide financial_total_capex_usd by financial_revenue_usd.",
            "Require a positive revenue denominator and compatible reporting periods, units, and boundaries.",
        ],
        "derived_feature_dependencies": [
            "financial_total_capex_usd",
            "financial_revenue_usd",
        ],
    },
    "emissions_intensity": {
        "source_JSON_path": "$derived.emissions_intensity",
        "data_type": "number",
        "canonical_unit": "tCO2e/USD",
        "category_or_context": "environmental",
        "validation_rules": [
            "Divide env_total_ghg_tco2e by financial_revenue_usd.",
            "Require a positive revenue denominator and compatible reporting periods, units, and boundaries.",
        ],
        "derived_feature_dependencies": [
            "env_total_ghg_tco2e",
            "financial_revenue_usd",
        ],
    },
}


def _primitive_source_path(feature_name: str, category: str) -> str:
    """Return the JSON path used by the existing validated extraction."""
    if category == "financial":
        return "$." + _FINANCIAL_SOURCE_FIELDS[feature_name] + ".value"
    return "$.values." + feature_name


def _build_registry() -> dict[str, dict]:
    """Build and validate the immutable registry from existing source schemas."""
    registry = {}
    for feature_name, source_definition in feature_schema()["fields"].items():
        source_category = source_definition["category"]
        category = _CATEGORY_NAMES[source_category]
        rules = [
            "Use only a validated existing extraction result.",
            "Require publication_date on or before prediction_as_of.",
            "Preserve null for missing, conflicting, or incompatible disclosures.",
            "Accept only the declared canonical unit.",
        ]
        if feature_name == "financial_employees_count":
            rules.append("Preserve employee headcount and do not relabel full-time equivalents.")
        definition = FeatureDefinition(
            feature_name=feature_name,
            source_JSON_path=_primitive_source_path(feature_name, source_category),
            data_type=source_definition["type"],
            canonical_unit=source_definition.get("unit"),
            category_or_context=category,
            validation_rules=rules,
            derived_feature_dependencies=[],
            categorical=False,
            predictive=True,
        )
        registry[feature_name] = definition.model_dump(mode="json")

    industry = FeatureDefinition(
        feature_name="industry",
        source_JSON_path="$.company.industry",
        data_type="string",
        canonical_unit=None,
        category_or_context="fixed_context",
        validation_rules=[
            "Use the company registry industry as categorical context.",
            "Keep company names, tickers, CIKs, and document identifiers out of predictive columns.",
        ],
        derived_feature_dependencies=[],
        categorical=True,
        predictive=True,
    )
    registry[industry.feature_name] = industry.model_dump(mode="json")

    for feature_name, source_definition in _DERIVED_FEATURES.items():
        definition = FeatureDefinition(
            feature_name=feature_name,
            categorical=False,
            predictive=True,
            **source_definition,
        )
        registry[feature_name] = definition.model_dump(mode="json")

    for name, definition in registry.items():
        if definition["feature_name"] != name:
            raise RuntimeError("Feature registry key does not match feature_name.")
        if definition["category_or_context"] not in APPROVED_FEATURE_CATEGORIES:
            raise RuntimeError("Feature registry contains an unsupported category.")
        for dependency in definition["derived_feature_dependencies"]:
            if dependency not in registry:
                raise RuntimeError(f"{name} depends on an unknown feature: {dependency}")
    return registry


_REGISTRY = _build_registry()


def feature_registry() -> dict[str, dict]:
    """Return a copy so callers cannot mutate the shared feature contract."""
    return deepcopy(_REGISTRY)


def predictive_feature_names() -> list[str]:
    """Return predictive features in stable registry order."""
    return [name for name, value in _REGISTRY.items() if value["predictive"]]


def numeric_feature_names() -> list[str]:
    """Return numeric, integer, and boolean predictive columns in stable order."""
    return [
        name
        for name, value in _REGISTRY.items()
        if value["predictive"] and not value["categorical"]
    ]


def categorical_feature_names() -> list[str]:
    """Return categorical predictive columns in stable order."""
    return [
        name
        for name, value in _REGISTRY.items()
        if value["predictive"] and value["categorical"]
    ]


def derived_feature_names() -> list[str]:
    """Return fields that are deterministically recomputed from primitive inputs."""
    return list(_DERIVED_FEATURES)
