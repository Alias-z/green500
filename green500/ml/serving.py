"""Serve one verified saved model run without reloading it for each request."""

from __future__ import annotations

import copy
import json
import math
import pickle
from datetime import date
from pathlib import Path

from green500.ml.artifacts import load_verified_run, model_path
from green500.ml.inference import (
    CATEGORY_ORDER,
    MODEL_FAMILIES,
    TARGETS,
    _active_coverage,
    _active_features,
    _feature_frame,
    _industry_warnings,
    _manifest,
    _ordered_features,
    _range_warnings,
    _require_row_availability_policy,
    _saved_availability_policy,
    _validate_model_features,
)
from green500.ml.personalization import (
    WEIGHTED_CATEGORIES,
    _identity_ebm,
    _scalar_intercept,
    _term_contract,
    validate_weights,
)

_CATEGORY_LABELS = {
    "financial": "Financial",
    "social": "Social",
    "environmental": "Environmental",
    "climate_target": "Climate targets",
    "financial_target": "Financial targets",
    "fixed_context": "Company context",
}

_FEATURE_LABELS = {
    "financial_revenue_usd": "Revenue",
    "financial_employees_count": "Employees",
    "financial_total_capex_usd": "Total capital expenditure",
    "financial_green_transition_capex_usd": "Green transition capital expenditure",
    "financial_operating_income_usd": "Operating income",
    "financial_total_assets_usd": "Total assets",
    "financial_cash_and_equivalents_usd": "Cash and equivalents",
    "financial_total_debt_usd": "Total debt",
    "financial_operating_cash_flow_usd": "Operating cash flow",
    "env_total_ghg_tco2e": "Total greenhouse-gas emissions",
    "env_scope_1_tco2e": "Scope 1 emissions",
    "env_scope_2_location_based_tco2e": "Scope 2 emissions (location based)",
    "env_scope_2_market_based_tco2e": "Scope 2 emissions (market based)",
    "env_scope_3_total_tco2e": "Scope 3 emissions",
    "env_total_energy_mwh": "Total energy",
    "env_renewable_electricity_percent": "Renewable electricity",
    "env_water_withdrawal_m3": "Water withdrawal",
    "env_total_waste_tonnes": "Total waste",
    "env_waste_recycled_percent": "Waste recycled",
    "social_employees_count": "Employees",
    "social_employee_turnover_percent": "Employee turnover",
    "social_women_workforce_percent": "Women in the workforce",
    "social_employee_fatalities_count": "Employee fatalities",
    "social_contractor_fatalities_count": "Contractor fatalities",
    "social_recordable_injury_rate_per_200000_hours": (
        "Recordable injury rate per 200,000 hours"
    ),
    "social_recordable_injury_rate_per_1000000_hours": (
        "Recordable injury rate per 1,000,000 hours"
    ),
    "social_suppliers_audited_count": "Suppliers audited",
    "social_confirmed_violations_count": "Confirmed violations",
    "social_community_investment_usd": "Community investment",
    "climate_net_zero_target_year": "Net-zero target year",
    "climate_sbti_validated": "Science Based Targets initiative validated",
    "climate_target_scope_1_2_absolute_reduction_baseline_year": (
        "Scope 1+2 reduction baseline year"
    ),
    "climate_target_scope_1_2_absolute_reduction_target_year": (
        "Scope 1+2 reduction target year"
    ),
    "climate_target_scope_1_2_absolute_reduction_reduction_pct": (
        "Scope 1+2 target reduction"
    ),
    "climate_target_scope_1_2_absolute_reduction_progress_pct": (
        "Scope 1+2 target progress"
    ),
    "climate_target_scope_3_absolute_reduction_baseline_year": (
        "Scope 3 reduction baseline year"
    ),
    "climate_target_scope_3_absolute_reduction_target_year": (
        "Scope 3 reduction target year"
    ),
    "climate_target_scope_3_absolute_reduction_reduction_pct": (
        "Scope 3 target reduction"
    ),
    "climate_target_scope_3_absolute_reduction_progress_pct": (
        "Scope 3 target progress"
    ),
    "operating_margin": "Operating margin",
    "debt_assets": "Debt-to-assets ratio",
    "capex_revenue": "Capital expenditure-to-revenue ratio",
    "emissions_intensity": "Emissions intensity",
    "industry": "Industry",
}

_SIGNED_FINANCIAL_FEATURES = {
    "financial_operating_income_usd",
    "financial_operating_cash_flow_usd",
}


def _read_json_object(path: Path, description: str) -> dict:
    try:
        value = json.loads(path.read_bytes())
    except json.JSONDecodeError as error:
        raise ValueError(f"The saved {description} is invalid JSON.") from error
    if not isinstance(value, dict):
        raise TypeError(f"The saved {description} must be a JSON object.")
    return value


def _load_saved_model(path: Path) -> object:
    try:
        with path.open("rb") as stream:
            return pickle.load(stream)
    except (pickle.UnpicklingError, AttributeError, EOFError, ImportError) as error:
        raise ValueError(f"Saved model cannot be loaded: {path}") from error


def _finite_values(
    values: object, expected_count: int, description: str
) -> list[float]:
    try:
        result = [float(value) for value in values]
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"The saved {description} returned non-numeric predictions."
        ) from error
    if len(result) != expected_count:
        raise ValueError(f"The saved {description} returned the wrong number of rows.")
    if any(not math.isfinite(value) for value in result):
        raise ValueError(f"The saved {description} returned a non-finite value.")
    return result


def _feature_label(feature_name: str) -> str:
    label = _FEATURE_LABELS.get(feature_name)
    if label is not None:
        return label
    return feature_name.replace("_", " ").capitalize()


def _is_editable(definition: dict) -> bool:
    return (
        definition.get("category_or_context") != "fixed_context"
        and not definition.get("derived_feature_dependencies")
        and definition.get("data_type") in {"integer", "number"}
    )


def _validate_registry(registry_artifact: dict, manifest: dict) -> dict[str, dict]:
    registry = registry_artifact.get("features")
    if not isinstance(registry, dict):
        raise TypeError("The saved feature registry has no features object.")
    ordered_features = _ordered_features(manifest)
    if set(registry) != set(ordered_features):
        raise ValueError("The saved feature registry differs from ordered_features.")
    for feature_name in ordered_features:
        definition = registry[feature_name]
        if not isinstance(definition, dict):
            raise TypeError(
                f"The saved registry definition for {feature_name} is invalid."
            )
        if definition.get("feature_name") != feature_name:
            raise ValueError(
                f"The saved registry definition for {feature_name} is misnamed."
            )
        if (
            definition.get("category_or_context")
            != manifest["feature_categories"][feature_name]
        ):
            raise ValueError(
                f"The saved category for {feature_name} differs from the manifest."
            )
        rules = definition.get("validation_rules")
        if (
            not isinstance(rules, list)
            or not rules
            or any(not isinstance(rule, str) or not rule.strip() for rule in rules)
        ):
            raise ValueError(
                f"The saved validation rules for {feature_name} must be prose strings."
            )
    return registry


def _company_with_name_alias(company: object) -> dict:
    if not isinstance(company, dict):
        raise TypeError("A serving row has no company object.")
    result = copy.deepcopy(company)
    if "name" not in result and isinstance(result.get("company_name"), str):
        result["name"] = result["company_name"]
    return result


def _scenario_value(feature_name: str, value: float, definition: dict) -> int | float:
    if not math.isfinite(value):
        raise ValueError(f"{feature_name} scenario value must be finite.")
    data_type = definition.get("data_type")
    unit = definition.get("canonical_unit")
    requires_integer = data_type == "integer" or unit == "count"
    if requires_integer:
        if not float(value).is_integer():
            raise ValueError(f"{feature_name} scenario value must be a whole number.")
        normalized: int | float = int(value)
    elif data_type == "number":
        normalized = float(value)
    else:
        raise ValueError(f"{feature_name} is not an editable numeric feature.")

    if unit == "year" and not 1900 <= normalized <= 2100:
        raise ValueError(f"{feature_name} scenario year must be from 1900 to 2100.")
    if unit == "percent" and not 0 <= normalized <= 100:
        raise ValueError(f"{feature_name} scenario percentage must be from 0 to 100.")
    category = definition.get("category_or_context")
    must_be_nonnegative = (
        unit in {"count", "tCO2e", "MWh", "m3", "t"}
        or category in {"environmental", "social"}
        or (
            feature_name.startswith("financial_")
            and feature_name not in _SIGNED_FINANCIAL_FEATURES
        )
    )
    if must_be_nonnegative and normalized < 0:
        raise ValueError(f"{feature_name} scenario value cannot be negative.")
    return normalized


def _validate_target_year_order(features: dict) -> None:
    for scope in ("scope_1_2", "scope_3"):
        prefix = f"climate_target_{scope}_absolute_reduction_"
        baseline = features.get(prefix + "baseline_year")
        target = features.get(prefix + "target_year")
        if baseline is not None and target is not None and target <= baseline:
            raise ValueError(
                f"The {scope.replace('_', ' ')} target year must be after its baseline year."
            )


def _scenario_metadata(
    original_metadata: object,
    operation: str,
    amount: float,
    original_value: object,
    scenario_value: object,
    status: str,
) -> dict:
    if original_metadata is None:
        result = {}
    elif isinstance(original_metadata, dict):
        result = copy.deepcopy(original_metadata)
    else:
        raise TypeError("Feature metadata must be an object or null.")
    original_status = result.get("status")
    original_source_missing_status = result.get("source_missing_status")
    evidence = result.get("evidence")
    if evidence is None:
        result["evidence"] = {"scenario": True}
    elif isinstance(evidence, dict):
        result["evidence"] = {**copy.deepcopy(evidence), "scenario": True}
    else:
        result["evidence"] = {
            "original_evidence": copy.deepcopy(evidence),
            "scenario": True,
        }
    result["scenario_adjustment"] = {
        "operation": operation,
        "amount": amount,
        "original_value": original_value,
        "scenario_value": scenario_value,
        "status": status,
        "original_status": original_status,
        "original_source_missing_status": original_source_missing_status,
    }
    if status == "applied":
        result["status"] = "scenario"
        result["source_missing_status"] = "hypothetical"
    return result


class SavedModelService:
    """Keep one integrity-checked run and its four fitted estimators in memory."""

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir).resolve()
        verified_run = load_verified_run(self.run_dir)
        self.manifest = copy.deepcopy(_manifest(verified_run))
        self._availability_policy = _saved_availability_policy(
            self.run_dir, self.manifest
        )
        registry_artifact = _read_json_object(
            self.run_dir / "feature_registry.json", "feature registry"
        )
        self.registry = copy.deepcopy(
            _validate_registry(registry_artifact, self.manifest)
        )
        self._metrics = _read_json_object(self.run_dir / "metrics.json", "metrics")
        self._models: dict[tuple[str, str], object] = {}
        self._term_contracts: dict[str, list[tuple[str, str]]] = {}
        self._intercepts: dict[str, float] = {}
        for target in TARGETS:
            active_features = _active_features(self.manifest, target)
            families = (self.manifest.get("models") or {}).get(target)
            if not isinstance(families, dict):
                raise FileNotFoundError(f"No saved models are available for {target}.")
            for family in MODEL_FAMILIES:
                if family not in families:
                    raise FileNotFoundError(
                        f"No saved {family} model is available for {target}."
                    )
                model = _load_saved_model(model_path(self.run_dir, target, family))
                _validate_model_features(model, active_features)
                self._models[target, family] = model
            ebm = self._models[target, "ebm"]
            _identity_ebm(ebm)
            self._term_contracts[target] = _term_contract(
                ebm, active_features, self.manifest["feature_categories"]
            )
            self._intercepts[target] = _scalar_intercept(ebm)

    def _validate_target(self, target: str) -> list[str]:
        if target not in TARGETS:
            raise ValueError("target must be esg or csa.")
        return _active_features(self.manifest, target)

    def describe(self) -> dict:
        """Return the saved feature and evaluation contract used by the UI."""
        targets = {}
        for target in TARGETS:
            active_features = self._validate_target(target)
            targets[target] = {
                "active_features": list(active_features),
                "fields": [
                    {
                        "feature_name": feature_name,
                        "label": _feature_label(feature_name),
                        "canonical_unit": self.registry[feature_name].get(
                            "canonical_unit"
                        ),
                        "category_or_context": self.registry[feature_name].get(
                            "category_or_context"
                        ),
                        "editable": _is_editable(self.registry[feature_name]),
                    }
                    for feature_name in active_features
                ],
                "metrics": copy.deepcopy(self._metrics.get(target) or {}),
            }
        return {
            "run_id": self.manifest.get("run_id", self.run_dir.name),
            "targets": targets,
            "categories": [
                {"name": category, "label": _CATEGORY_LABELS[category]}
                for category in CATEGORY_ORDER
            ],
            "evaluation": {
                "dataset_snapshot_id": self.manifest.get("dataset_snapshot_id"),
                "targets": copy.deepcopy(self.manifest.get("targets") or {}),
            },
            "limitations": copy.deepcopy(self.manifest.get("limitations") or []),
        }

    def evaluate_rows(
        self,
        rows: list[dict],
        target: str,
        weights: dict[str, float] | None = None,
    ) -> list[dict]:
        """Evaluate rows in input order using the already loaded target models."""
        active_features = self._validate_target(target)
        if not isinstance(rows, list):
            raise TypeError("rows must be a list.")
        if not rows:
            return []
        normalized_weights = validate_weights(weights)
        frames = []
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("Each serving row must be an object.")
            _require_row_availability_policy(row, self._availability_policy)
            try:
                date.fromisoformat(str(row["prediction_as_of"]))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    "Each serving row needs a valid YYYY-MM-DD prediction_as_of date."
                ) from error
            features = row.get("features")
            if not isinstance(features, dict):
                raise TypeError("A serving row has no features object.")
            frames.append(_feature_frame(features, self.manifest, active_features))

        try:
            import pandas as pd
        except ImportError as error:
            raise RuntimeError("pandas is required for saved-model serving.") from error
        frame = pd.concat(frames, ignore_index=True)
        ebm = self._models[target, "ebm"]
        catboost = self._models[target, "catboost"]
        ebm_predictions = _finite_values(ebm.predict(frame), len(rows), "EBM")
        catboost_predictions = _finite_values(
            catboost.predict(frame, thread_count=1), len(rows), "CatBoost"
        )
        term_values = ebm.eval_terms(frame)
        if len(term_values) != len(rows):
            raise ValueError("The EBM returned the wrong number of contribution rows.")

        term_contract = self._term_contracts[target]
        intercept = self._intercepts[target]
        results = []
        for row_index, row in enumerate(rows):
            terms_for_row = term_values[row_index]
            if len(terms_for_row) != len(term_contract):
                raise ValueError("The EBM term output does not match term_features_.")
            feature_contributions = {}
            category_contributions = {category: 0.0 for category in WEIGHTED_CATEGORIES}
            fixed_context_contribution = 0.0
            for term_index, (feature_name, category) in enumerate(term_contract):
                contribution = float(terms_for_row[term_index])
                if not math.isfinite(contribution):
                    raise ValueError(
                        "The EBM returned a non-finite local contribution."
                    )
                feature_contributions[feature_name] = contribution
                if category == "fixed_context":
                    fixed_context_contribution += contribution
                else:
                    category_contributions[category] += contribution
            reconstructed = (
                intercept
                + fixed_context_contribution
                + sum(category_contributions.values())
            )
            if not math.isclose(
                reconstructed,
                ebm_predictions[row_index],
                rel_tol=1e-9,
                abs_tol=1e-8,
            ):
                raise ValueError("Signed EBM terms do not reconstruct its prediction.")
            personalized_index = (
                intercept
                + fixed_context_contribution
                + sum(
                    normalized_weights[category] * category_contributions[category]
                    for category in WEIGHTED_CATEGORIES
                )
            )
            if not math.isfinite(personalized_index):
                raise ValueError("The personalized index is non-finite.")

            source_features = row["features"]
            source_metadata = row.get("metadata")
            if source_metadata is None:
                source_metadata = {}
            if not isinstance(source_metadata, dict):
                raise TypeError("A serving row's metadata must be an object.")
            warnings = [
                *list(row.get("warnings") or []),
                *_range_warnings(source_features, self.manifest, active_features),
            ]
            if "industry" in active_features:
                warnings.extend(_industry_warnings(source_features, self.manifest))
            missing = [
                name for name in active_features if source_features.get(name) is None
            ]
            if missing:
                warnings.append(
                    f"{len(missing)} active model inputs are missing; saved missing-value behavior was used."
                )
            results.append(
                {
                    "company": _company_with_name_alias(row.get("company")),
                    "features": {
                        name: copy.deepcopy(source_features[name])
                        for name in active_features
                    },
                    "metadata": {
                        name: copy.deepcopy(source_metadata.get(name))
                        for name in active_features
                    },
                    "predictions": {
                        "ebm": ebm_predictions[row_index],
                        "catboost": catboost_predictions[row_index],
                    },
                    "personalized_index": personalized_index,
                    "intercept": intercept,
                    "fixed_context_contribution": fixed_context_contribution,
                    "feature_contributions": feature_contributions,
                    "category_contributions": category_contributions,
                    "coverage": _active_coverage(row, self.manifest, target),
                    "warnings": list(dict.fromkeys(warnings)),
                }
            )
        return results

    def predict_rows_only(
        self, rows: list[dict], target: str
    ) -> list[dict[str, float]]:
        """Batch both saved predictions without building explanations or copying evidence."""
        active_features = self._validate_target(target)
        if not isinstance(rows, list):
            raise TypeError("rows must be a list.")
        if not rows:
            return []
        records = []
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("Each serving row must be an object.")
            _require_row_availability_policy(row, self._availability_policy)
            features = row.get("features")
            if not isinstance(features, dict):
                raise TypeError("A serving row has no features object.")
            missing = [name for name in active_features if name not in features]
            if missing:
                raise ValueError(
                    "A serving row is missing active features: " + ", ".join(missing)
                )
            records.append({name: features[name] for name in active_features})
        try:
            import pandas as pd
        except ImportError as error:
            raise RuntimeError("pandas is required for saved-model serving.") from error
        frame = pd.DataFrame.from_records(records, columns=active_features)
        categorical = set(self.manifest.get("categorical_features") or []).intersection(
            active_features
        )
        for feature_name in active_features:
            if feature_name in categorical:
                frame[feature_name] = frame[feature_name].map(
                    lambda value: (
                        value.strip()
                        if isinstance(value, str) and value.strip()
                        else "Unknown"
                    )
                )
                continue
            converted = pd.to_numeric(frame[feature_name], errors="coerce")
            invalid = frame[feature_name].notna() & converted.isna()
            if invalid.any():
                raise ValueError(
                    f"Numeric feature contains a non-numeric value: {feature_name}"
                )
            if any(not math.isfinite(float(value)) for value in converted.dropna()):
                raise ValueError(
                    f"Numeric feature contains a non-finite value: {feature_name}"
                )
            frame[feature_name] = converted.astype(float)
        ebm_values = _finite_values(
            self._models[target, "ebm"].predict(frame), len(rows), "EBM"
        )
        catboost_values = _finite_values(
            self._models[target, "catboost"].predict(frame, thread_count=1),
            len(rows),
            "CatBoost",
        )
        return [
            {"ebm": ebm, "catboost": catboost}
            for ebm, catboost in zip(ebm_values, catboost_values, strict=True)
        ]

    def apply_scenario(
        self,
        rows: list[dict],
        target: str,
        adjustments: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """Apply validated numeric changes to copied rows and recompute ratios."""
        active_features = set(self._validate_target(target))
        if not isinstance(rows, list):
            raise TypeError("rows must be a list.")
        if not isinstance(adjustments, list):
            raise TypeError("adjustments must be a list.")
        if not adjustments:
            return copy.deepcopy(rows), []
        normalized_adjustments = []
        seen_features = set()
        for adjustment in adjustments:
            if not isinstance(adjustment, dict) or set(adjustment) != {
                "feature_name",
                "operation",
                "value",
            }:
                raise ValueError(
                    "Each adjustment needs feature_name, operation, and value only."
                )
            feature_name = adjustment["feature_name"]
            operation = adjustment["operation"]
            amount = adjustment["value"]
            if not isinstance(feature_name, str) or not feature_name:
                raise ValueError("Adjustment feature_name must be a non-empty string.")
            if feature_name in seen_features:
                raise ValueError(f"Duplicate scenario adjustment: {feature_name}.")
            seen_features.add(feature_name)
            if feature_name not in active_features:
                raise ValueError(f"{feature_name} is not active for target {target}.")
            definition = self.registry[feature_name]
            if definition.get("category_or_context") == "fixed_context":
                raise ValueError(
                    f"{feature_name} is fixed context and cannot be changed."
                )
            if definition.get("derived_feature_dependencies"):
                raise ValueError(
                    f"{feature_name} is derived; change a primitive input instead."
                )
            if not _is_editable(definition):
                raise ValueError(f"{feature_name} is not an editable numeric feature.")
            if operation not in {"add", "percent", "set"}:
                raise ValueError(
                    f"{feature_name} operation must be add, percent, or set."
                )
            if (
                isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(amount)
            ):
                raise ValueError(f"{feature_name} adjustment value must be finite.")
            normalized_adjustments.append(
                (feature_name, operation, float(amount), definition)
            )

        from green500.ml.dataset import recompute_derived_features

        scenario_rows = copy.deepcopy(rows)
        changes = []
        for scenario_row in scenario_rows:
            if not isinstance(scenario_row, dict):
                raise TypeError("Each serving row must be an object.")
            features = scenario_row.get("features")
            metadata = scenario_row.get("metadata")
            if not isinstance(features, dict):
                raise TypeError("A serving row has no features object.")
            if not isinstance(metadata, dict):
                raise TypeError("A serving row has no metadata object.")
            company = scenario_row.get("company")
            if not isinstance(company, dict) or not isinstance(
                company.get("company_cik"), str
            ):
                raise TypeError("A scenario row needs company.company_cik.")
            company_cik = company["company_cik"]
            for feature_name, operation, amount, definition in normalized_adjustments:
                if feature_name not in features:
                    raise ValueError(f"A scenario row is missing {feature_name}.")
                original_value = features[feature_name]
                if original_value is not None and (
                    isinstance(original_value, bool)
                    or not isinstance(original_value, (int, float))
                    or not math.isfinite(original_value)
                ):
                    raise ValueError(
                        f"The original {feature_name} value must be finite or null."
                    )
                if original_value is None and operation in {"add", "percent"}:
                    scenario_value = None
                    status = "missing_original_value"
                else:
                    if operation == "set":
                        calculated_value = amount
                    elif operation == "add":
                        calculated_value = float(original_value) + amount
                    else:
                        calculated_value = float(original_value) * (1 + amount / 100)
                    scenario_value = _scenario_value(
                        feature_name, calculated_value, definition
                    )
                    status = "applied"
                    features[feature_name] = scenario_value
                metadata[feature_name] = _scenario_metadata(
                    metadata.get(feature_name),
                    operation,
                    amount,
                    original_value,
                    scenario_value,
                    status,
                )
                changes.append(
                    {
                        "company_cik": company_cik,
                        "feature_name": feature_name,
                        "original_value": original_value,
                        "scenario_value": scenario_value,
                        "operation": operation,
                        "amount": amount,
                        "status": status,
                    }
                )
            _validate_target_year_order(features)
            updated_features, updated_metadata = recompute_derived_features(
                features, metadata
            )
            scenario_row["features"] = updated_features
            scenario_row["metadata"] = updated_metadata
            scenario_row["warnings"] = list(
                dict.fromkeys(
                    [
                        *list(scenario_row.get("warnings") or []),
                        "Scenario results describe model sensitivity, not a guaranteed score change.",
                    ]
                )
            )
        return scenario_rows, changes
