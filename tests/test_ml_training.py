"""Smoke-test the required model families without presenting synthetic scores as results."""

from copy import deepcopy
from datetime import date

from catboost import CatBoostRegressor
from interpret.glassbox import ExplainableBoostingRegressor

from green500.ml.artifacts import load_model_artifact, load_verified_run
from green500.ml.config import load_ml_config
from green500.ml.contracts import CompanyRecord, FeatureObservation, LabelRecord
from green500.ml.dataset import build_dataset_from_records, export_dataset_snapshot
from green500.ml.splits import build_split_assignments
from green500.ml.training import (
    _aligned_rows,
    _target_training,
    model_configuration_grid,
    train_models,
)


def _synthetic_dataset(row_count=45):
    companies, labels, features, metadata = [], [], [], []
    for index in range(row_count):
        row_id = f"synthetic-{index:03d}"
        industry = ("energy", "technology", "health care")[index % 3]
        revenue = None if index % 11 == 0 else float(100 + index)
        emissions = float(200 - index * 2)
        companies.append(
            {
                "row_id": row_id,
                "company_cik": f"{index:010d}",
                "ticker": f"S{index}",
                "company_name": f"Synthetic company {index}",
                "assessment_cycle": "synthetic-smoke",
                "prediction_as_of": "2026-06-30",
            }
        )
        features.append(
            {
                "financial_revenue_usd": revenue,
                "env_total_ghg_tco2e": emissions,
                "industry": industry,
            }
        )
        labels.append(
            {
                "row_id": row_id,
                "esg_score": float(25 + index % 50),
                "esg_label_published_at": "2026-05-01",
                "esg_authorization_reference": "synthetic-smoke-only",
                "csa_score": None,
                "csa_label_published_at": None,
                "csa_authorization_reference": None,
            }
        )
        metadata.append(
            {
                "row_id": row_id,
                "features": {
                    "financial_revenue_usd": {
                        "status": "missing" if revenue is None else "reported",
                        "source_missing_status": (
                            "not_disclosed" if revenue is None else "observed"
                        ),
                        "missing_reason": (
                            "The company did not disclose revenue."
                            if revenue is None
                            else None
                        ),
                    }
                },
            }
        )
    definitions = {
        "financial_revenue_usd": {
            "category_or_context": "financial",
            "data_type": "number",
        },
        "env_total_ghg_tco2e": {
            "category_or_context": "environmental",
            "data_type": "number",
        },
        "industry": {"category_or_context": "fixed_context", "data_type": "string"},
    }
    return {
        "snapshot_id": "synthetic-smoke-only",
        "companies": companies,
        "X": features,
        "labels": labels,
        "metadata": metadata,
        "schema": {
            "numeric_features": ["financial_revenue_usd", "env_total_ghg_tco2e"],
            "categorical_features": ["industry"],
            "feature_definitions": definitions,
        },
    }


def _small_training_config():
    config = deepcopy(load_ml_config("config/ml.yaml"))
    config["training"]["minimum_training_rows"] = 5
    config["training"]["feature_selection"]["minimum_observed_companies"] = 5
    config["training"]["feature_selection"]["minimum_continuous_distinct_values"] = 2
    config["training"]["models"]["ebm"]["fixed"]["min_samples_leaf"] = 2
    config["training"]["models"]["ebm"]["search"] = {
        "max_bins": [8, 16],
        "max_rounds": [5, 10],
    }
    config["training"]["models"]["catboost"]["search"] = {
        "depth": [2, 3],
        "iterations": [5, 10],
    }
    return config


def test_default_search_has_four_required_configurations_per_family():
    config = load_ml_config("config/ml.yaml")

    ebm = model_configuration_grid(config, "ebm")
    catboost = model_configuration_grid(config, "catboost")

    assert len(ebm) == len(catboost) == 4
    assert {
        (row["parameters"]["max_bins"], row["parameters"]["max_rounds"]) for row in ebm
    } == {
        (32, 500),
        (32, 1500),
        (64, 500),
        (64, 1500),
    }
    assert {
        (row["parameters"]["depth"], row["parameters"]["iterations"])
        for row in catboost
    } == {
        (4, 300),
        (4, 800),
        (6, 300),
        (6, 800),
    }
    assert all(row["parameters"]["interactions"] == 0 for row in ebm)
    assert all(row["parameters"]["early_stopping_rounds"] == 0 for row in ebm)


def test_actual_ebm_and_catboost_share_one_split_and_preserve_missing_values():
    dataset = _synthetic_dataset()
    config = _small_training_config()
    split = build_split_assignments(dataset, "esg", config, "snapshot")
    ordered = ["financial_revenue_usd", "env_total_ghg_tco2e", "industry"]
    aligned = _aligned_rows(dataset, ordered)

    result = _target_training(
        aligned,
        "esg",
        split,
        config,
        ordered,
        ["financial_revenue_usd", "env_total_ghg_tco2e"],
        ["industry"],
        dataset["schema"]["feature_definitions"],
        {
            name: definition["category_or_context"]
            for name, definition in dataset["schema"]["feature_definitions"].items()
        },
    )

    assert isinstance(result["models"]["ebm"], ExplainableBoostingRegressor)
    assert isinstance(result["models"]["catboost"], CatBoostRegressor)
    assert set(result["selected"]) == {"ebm", "catboost"}
    assert len(result["cv_predictions"]) == 4 * 8 * len(split["development_row_ids"])
    assert {row["row_id"] for row in result["test_predictions"]} == set(
        split["final_test_row_ids"]
    )
    assert result["metrics"]["final_test"]["models"]["ebm"]["matched_count"] == len(
        split["final_test_row_ids"]
    )
    assert (
        result["metrics"]["final_test"]["baselines"]["last_published_company_score"][
            "matched_count"
        ]
        == 0
    )
    assert result["feature_selection"]["selection"]["feature_set"] == "core"
    assert result["feature_selection"]["final_mask"]["active_features"] == ordered


def test_missing_authorized_labels_block_training_without_creating_a_run(
    monkeypatch, tmp_path
):
    dataset = _synthetic_dataset()
    for row in dataset["labels"]:
        row["esg_score"] = None
        row["esg_authorization_reference"] = None
    monkeypatch.setattr(
        "green500.ml.training._load_dataset", lambda _: (dataset, tmp_path)
    )

    result = train_models(
        "unused", load_ml_config("config/ml.yaml"), output_dir=tmp_path / "runs"
    )

    assert result["status"] == "blocked"
    assert result["run_id"] is None
    assert result["targets"]["esg"]["status"] == "blocked"
    assert result["targets"]["csa"]["status"] == "blocked"
    assert not (tmp_path / "runs").exists()


def test_successful_synthetic_run_saves_verified_actual_models(monkeypatch, tmp_path):
    companies, observations, labels = [], [], []
    for index in range(45):
        cik = f"{index:010d}"
        companies.append(
            CompanyRecord(
                company_cik=cik,
                ticker=f"S{index}",
                company_name=f"Synthetic company {index}",
                industry=("Energy", "Technology", "Health Care")[index % 3],
            )
        )
        for feature_name, value, unit in (
            ("financial_revenue_usd", float(100 + index), "USD"),
            ("env_total_ghg_tco2e", float(200 - index), "tCO2e"),
        ):
            observations.append(
                FeatureObservation(
                    company_cik=cik,
                    feature_name=feature_name,
                    value=value,
                    unit=unit,
                    reporting_year=2025,
                    publication_date=date(2026, 1, 15),
                    status="reported",
                    source_url="https://example.com/synthetic-source",
                )
            )
        labels.append(
            LabelRecord(
                company_cik=cik,
                target_name="esg",
                assessment_cycle="synthetic-smoke",
                prediction_as_of=date(2026, 6, 30),
                score=float(25 + index % 50),
                label_published_at=date(2026, 5, 1),
                source_name="Synthetic smoke fixture",
                source_url="https://example.com/synthetic-label",
                source_date=date(2026, 5, 1),
                source_sha256="0" * 64,
                authorization_reference="synthetic-smoke-only",
            )
        )
    dataset = build_dataset_from_records(companies, observations, labels)
    snapshot = export_dataset_snapshot(dataset, tmp_path / "datasets")

    from green500.ml import training

    original_grid = training.model_configuration_grid

    def small_grid(config, family):
        grid = original_grid(config, family)
        for index, configuration in enumerate(grid):
            if family == "ebm":
                configuration["parameters"]["max_rounds"] = 5 + index
            else:
                configuration["parameters"]["iterations"] = 5 + index
        return grid

    monkeypatch.setattr(training, "model_configuration_grid", small_grid)
    result = train_models(
        snapshot["snapshot_dir"],
        "config/ml.yaml",
        output_dir=tmp_path / "runs",
        split_mode="snapshot",
    )

    assert result["status"] == "partial"
    assert result["targets"]["esg"]["status"] == "trained"
    assert result["targets"]["csa"]["status"] == "blocked"
    manifest = load_verified_run(result["run_dir"])
    assert set(manifest["models"]["esg"]) == {"ebm", "catboost"}
    assert manifest["active_features"]["esg"] == [
        feature
        for feature in manifest["ordered_features"]
        if feature in set(manifest["active_features"]["esg"])
    ]
    assert set(manifest["ablation_results"]["esg"]) == {
        "core",
        "core_plus_climate_target",
        "core_plus_financial_target",
        "core_plus_both",
    }
    assert isinstance(
        load_model_artifact(result["run_dir"], "esg", "ebm"),
        ExplainableBoostingRegressor,
    )
    assert isinstance(
        load_model_artifact(result["run_dir"], "esg", "catboost"),
        CatBoostRegressor,
    )
    assert (tmp_path / "runs" / "latest.json").is_file()
