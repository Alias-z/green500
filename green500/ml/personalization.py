"""Rank companies from signed EBM terms and explicit category multipliers."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from green500.ml.inference import (
    _active_coverage,
    _active_features,
    _excluded_features,
    _feature_frame,
    _industry_warnings,
    _prediction_date,
    _range_warnings,
    _validate_model_features,
    load_prediction_run,
    resolve_model_run,
)

WEIGHTED_CATEGORIES = (
    "financial",
    "social",
    "environmental",
    "climate_target",
    "financial_target",
)


def validate_weights(weights: dict[str, float] | None) -> dict[str, float]:
    """Fill omitted multipliers with one and enforce the published range."""
    supplied = {} if weights is None else weights
    if not isinstance(supplied, dict):
        raise TypeError("Category weights must be an object.")
    unknown = sorted(set(supplied) - set(WEIGHTED_CATEGORIES))
    if unknown:
        raise ValueError("Unknown category weights: " + ", ".join(unknown))
    result = {}
    for category in WEIGHTED_CATEGORIES:
        value = supplied.get(category, 1.0)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 3
        ):
            raise ValueError(f"{category} weight must be a finite number from 0 to 3.")
        result[category] = float(value)
    return result


def _identity_ebm(model: Any) -> None:
    link = getattr(model, "link_", None)
    if link != "identity":
        raise ValueError("Personalized ranking requires an EBM with an identity link.")
    get_params = getattr(model, "get_params", None)
    if callable(get_params):
        interactions = get_params(deep=False).get("interactions")
        if interactions != 0:
            raise ValueError("Personalized ranking requires EBM interactions=0.")


def _scalar_intercept(model: Any) -> float:
    intercept = getattr(model, "intercept_", None)
    try:
        if hasattr(intercept, "reshape"):
            flattened = intercept.reshape(-1)
            if len(flattened) != 1:
                raise ValueError
            value = float(flattened[0])
        elif isinstance(intercept, (list, tuple)):
            if len(intercept) != 1:
                raise ValueError
            value = float(intercept[0])
        else:
            value = float(intercept)
    except (TypeError, ValueError) as error:
        raise ValueError("The EBM artifact has no scalar intercept.") from error
    if not math.isfinite(value):
        raise ValueError("The EBM artifact has a non-finite intercept.")
    return value


def _term_contract(model: Any, ordered_features: list[str], categories: dict) -> list[tuple[str, str]]:
    terms = getattr(model, "term_features_", None)
    if not isinstance(terms, (list, tuple)):
        raise TypeError("The EBM artifact has no term_features_ contract.")
    contract = []
    for term in terms:
        if not isinstance(term, (list, tuple)) or len(term) != 1:
            raise ValueError("Personalized ranking does not accept interaction terms.")
        feature_index = term[0]
        if (
            isinstance(feature_index, bool)
            or not isinstance(feature_index, int)
            or not 0 <= feature_index < len(ordered_features)
        ):
            raise ValueError("The EBM artifact contains an invalid term feature index.")
        feature = ordered_features[feature_index]
        category = categories.get(feature)
        if category not in {*WEIGHTED_CATEGORIES, "fixed_context"}:
            raise ValueError(f"Feature {feature} has no supported category.")
        contract.append((feature, category))
    return contract


def personalize_feature_rows(
    rows: list[dict],
    weights: dict[str, float] | None,
    target: str,
    model_run: str | Path,
) -> dict:
    """Score dated rows from local signed EBM contributions without mutation."""
    if target not in {"esg", "csa"}:
        raise ValueError("target must be esg or csa.")
    if not rows:
        raise ValueError("Ranking needs at least one company row.")
    prediction_dates = {row.get("prediction_as_of") for row in rows}
    if len(prediction_dates) != 1 or None in prediction_dates:
        raise ValueError("Ranking rows must share one prediction_as_of date.")
    normalized_weights = validate_weights(weights)
    run_dir, _, manifest = load_prediction_run(model_run)
    entry = ((manifest.get("models") or {}).get(target) or {}).get("ebm")
    if not entry:
        raise FileNotFoundError(f"No saved EBM model is available for {target}.")
    from green500.ml.artifacts import load_model_artifact

    model = load_model_artifact(run_dir, target, "ebm")
    _identity_ebm(model)
    ordered = _active_features(manifest, target)
    categories = manifest["feature_categories"]
    exclusions = _excluded_features(manifest, target)
    _validate_model_features(model, ordered)
    term_contract = _term_contract(model, ordered, categories)
    intercept = _scalar_intercept(model)

    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError("pandas is required for personalized ranking.") from error
    frames = []
    for row in rows:
        features = row.get("features")
        if not isinstance(features, dict):
            raise TypeError("A ranking row has no features object.")
        frames.append(_feature_frame(features, manifest, ordered))
    frame = pd.concat(frames, ignore_index=True)
    term_values = model.eval_terms(frame)
    predictions = model.predict(frame)
    if len(term_values) != len(rows) or len(predictions) != len(rows):
        raise ValueError("The EBM returned the wrong number of ranking rows.")

    ranked = []
    for row_index, row in enumerate(rows):
        contributions = {category: 0.0 for category in WEIGHTED_CATEGORIES}
        fixed_context = 0.0
        terms_for_row = term_values[row_index]
        if len(terms_for_row) != len(term_contract):
            raise ValueError("The EBM term output does not match term_features_.")
        feature_contributions = {}
        for term_index, (feature, category) in enumerate(term_contract):
            contribution = float(terms_for_row[term_index])
            if not math.isfinite(contribution):
                raise ValueError("The EBM returned a non-finite local contribution.")
            feature_contributions[feature] = contribution
            if category == "fixed_context":
                fixed_context += contribution
            else:
                contributions[category] += contribution
        raw_prediction = float(predictions[row_index])
        reconstructed = intercept + fixed_context + sum(contributions.values())
        if not math.isclose(
            reconstructed, raw_prediction, rel_tol=1e-9, abs_tol=1e-8
        ):
            raise ValueError("Signed EBM terms do not reconstruct its prediction.")
        personalized = intercept + fixed_context + sum(
            normalized_weights[category] * contributions[category]
            for category in WEIGHTED_CATEGORIES
        )
        features = row["features"]
        warnings = [
            *list(row.get("warnings") or []),
            *_range_warnings(features, manifest, ordered),
        ]
        if "industry" in ordered:
            warnings.extend(_industry_warnings(features, manifest))
        if any(features.get(feature) is None for feature in ordered):
            warnings.append(
                "Missing-value EBM contributions affect this personalized index."
            )
        category_details = {}
        for category in WEIGHTED_CATEGORIES:
            active_in_category = [
                feature for feature in ordered if categories[feature] == category
            ]
            excluded_in_category = {
                feature: details
                for feature, details in exclusions.items()
                if categories[feature] == category
            }
            category_details[category] = {
                "contribution": contributions[category],
                "active_features": active_in_category,
                "excluded_features": excluded_in_category,
                "category_excluded_from_model": not active_in_category,
            }
        ranked.append(
            {
                "company": row.get("company"),
                "row_id": row.get("row_id"),
                "prediction_as_of": row.get("prediction_as_of"),
                "raw_ebm_prediction": raw_prediction,
                "personalized_index": personalized,
                "intercept": intercept,
                "fixed_context_contribution": fixed_context,
                "category_contributions": contributions,
                "category_details": category_details,
                "feature_contributions": feature_contributions,
                "coverage": row.get("coverage") or {},
                "model_feature_coverage": _active_coverage(row, manifest, target),
                "warnings": list(dict.fromkeys(warnings)),
            }
        )
    ranked.sort(
        key=lambda item: (
            -item["personalized_index"],
            str((item.get("company") or {}).get("company_cik", "")),
        )
    )
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank
    return {
        "label": "Personalized Green500 index",
        "target": target,
        "model_version": manifest.get("run_id", run_dir.name),
        "prediction_as_of": rows[0].get("prediction_as_of"),
        "weights": normalized_weights,
        "feature_selection": {
            "active_features": ordered,
            "excluded_features": exclusions,
        },
        "companies": ranked,
        "disclosure": (
            "This is a personalized model index, not an official score or an "
            "independently measured environmental impact."
        ),
    }


def _cohort_company_ids(settings: Any, cohort: object) -> list[str]:
    if isinstance(cohort, (list, tuple, set)):
        company_ids = [str(value) for value in cohort]
        if not company_ids:
            raise ValueError("The requested cohort is empty.")
        return list(dict.fromkeys(company_ids))
    from green500 import db

    industry = None
    if cohort is not None:
        if not isinstance(cohort, dict) or set(cohort) - {"industry", "company_ids"}:
            raise ValueError("cohort must contain industry or company_ids.")
        if cohort.get("company_ids") is not None:
            return _cohort_company_ids(settings, cohort["company_ids"])
        industry = cohort.get("industry")
        if not isinstance(industry, str) or not industry.strip():
            raise ValueError("cohort industry must be a non-empty string.")
    with db.connect(settings) as connection:
        if industry is None:
            records = connection.execute(
                "SELECT cik FROM companies WHERE is_current ORDER BY cik"
            ).fetchall()
        else:
            records = connection.execute(
                "SELECT cik FROM companies WHERE is_current AND sector=%s ORDER BY cik",
                (industry,),
            ).fetchall()
    company_ids = [record["cik"] for record in records]
    if not company_ids:
        raise ValueError("The requested cohort contains no current companies.")
    return company_ids


def _build_default_cohort_rows(
    settings: Any,
    company_ids: list[str],
    prediction_as_of: str,
    assessment_cycle: str,
) -> list[dict]:
    """Load source records once when ranking more than one company."""
    from green500.ml.contracts import training_row_id
    from green500.ml.dataset import _build_feature_row, load_current_feature_records

    companies, observations = load_current_feature_records(settings)
    observations_by_cik = {}
    for observation in observations:
        observations_by_cik.setdefault(observation.company_cik, []).append(observation)
    prediction_date = date.fromisoformat(prediction_as_of)
    rows = []
    for company_id in company_ids:
        normalized_id = company_id.strip().casefold()
        matches = [
            company
            for company in companies
            if company.company_cik == company_id.zfill(10)
            or company.ticker.casefold() == normalized_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Company {company_id!r} must match exactly one current CIK or ticker."
            )
        company = matches[0]
        features, metadata, coverage, quarantined = _build_feature_row(
            company,
            observations_by_cik.get(company.company_cik, []),
            prediction_date,
        )
        warnings = sorted({item["reason"] for item in quarantined})
        warnings.append(
            "Industry is current fixed context because no historical effective date is stored."
        )
        rows.append(
            {
                "row_id": training_row_id(
                    company.company_cik, assessment_cycle, prediction_date
                ),
                "company": company.model_dump(mode="json"),
                "assessment_cycle": assessment_cycle,
                "prediction_as_of": prediction_as_of,
                "features": features,
                "metadata": metadata,
                "coverage": coverage,
                "warnings": warnings,
            }
        )
    return rows


def rank_companies(
    weights: dict[str, float] | None,
    target: str = "esg",
    cohort: object = None,
    *,
    prediction_as_of: str | date,
    settings: Any | None = None,
    model_run: str | Path | None = None,
    assessment_cycle: str = "inference",
    row_builder: Callable[..., dict] | None = None,
) -> dict:
    """Build a cohort's dated rows and rank its EBM category contributions."""
    if settings is None:
        from green500.config import load_settings

        settings = load_settings()
    as_of = _prediction_date(prediction_as_of)
    company_ids = _cohort_company_ids(settings, cohort)
    if row_builder is None:
        rows = _build_default_cohort_rows(
            settings, company_ids, as_of, assessment_cycle
        )
    else:
        rows = [
            row_builder(
                settings,
                company_id,
                as_of,
                assessment_cycle=assessment_cycle,
            )
            for company_id in company_ids
        ]
    return personalize_feature_rows(
        rows, weights, target, resolve_model_run(settings, model_run)
    )
