"""Verify company-group and chronological model evaluation boundaries."""

from datetime import date

from green500.ml.config import load_ml_config
from green500.ml.splits import build_split_assignments


def _config():
    return load_ml_config("config/ml.yaml")


def _snapshot_dataset(company_count=15, rows_per_company=2):
    companies, labels, features = [], [], []
    for company_index in range(company_count):
        for row_index in range(rows_per_company):
            row_id = f"row-{company_index:02d}-{row_index}"
            companies.append(
                {
                    "row_id": row_id,
                    "company_cik": f"{company_index:010d}",
                    "assessment_cycle": f"snapshot-{row_index}",
                    "prediction_as_of": "2026-06-30",
                }
            )
            labels.append(
                {
                    "row_id": row_id,
                    "esg_score": float(company_index + row_index),
                    "esg_label_published_at": "2026-05-01",
                    "esg_authorization_reference": "licensed-fixture",
                    "csa_score": (
                        float(company_index + row_index)
                        if company_index < company_count - 3
                        else None
                    ),
                    "csa_label_published_at": "2026-05-01",
                    "csa_authorization_reference": "licensed-fixture",
                }
            )
            features.append({})
    return {
        "snapshot_id": "snapshot-test",
        "companies": companies,
        "labels": labels,
        "X": features,
    }


def _groups(row_ids, dataset):
    by_id = {row["row_id"]: row["company_cik"] for row in dataset["companies"]}
    return {by_id[row_id] for row_id in row_ids}


def test_snapshot_split_keeps_company_groups_out_of_every_scoring_partition():
    dataset = _snapshot_dataset()
    split = build_split_assignments(dataset, "esg", _config(), "snapshot")

    development_groups = _groups(split["development_row_ids"], dataset)
    final_test_groups = _groups(split["final_test_row_ids"], dataset)
    assert split["mode"] == "snapshot_estimation"
    assert split["seed"] == 42
    assert development_groups.isdisjoint(final_test_groups)
    assert len(split["folds"]) == 3
    for fold in split["folds"]:
        assert _groups(fold["train_row_ids"], dataset).isdisjoint(
            _groups(fold["validation_row_ids"], dataset)
        )
        assert set(fold["train_row_ids"]) | set(fold["validation_row_ids"]) == set(
            split["development_row_ids"]
        )

    repeated = build_split_assignments(dataset, "esg", _config(), "snapshot")
    assert repeated == split


def test_target_eligibility_is_independent_while_each_target_reuses_its_split():
    dataset = _snapshot_dataset()

    esg = build_split_assignments(dataset, "esg", _config(), "snapshot")
    csa = build_split_assignments(dataset, "csa", _config(), "snapshot")

    assert len(esg["eligible_row_ids"]) == 30
    assert len(csa["eligible_row_ids"]) == 24
    assert csa["excluded_row_counts"]["missing_or_invalid_label"] == 6
    assert build_split_assignments(dataset, "csa", _config(), "snapshot") == csa


def test_historical_split_uses_expanding_cycles_and_public_training_labels():
    companies, labels, features = [], [], []
    for cycle in range(2020, 2024):
        for company_index in range(4):
            row_id = f"{cycle}-{company_index}"
            companies.append(
                {
                    "row_id": row_id,
                    "company_cik": f"{company_index:010d}",
                    "assessment_cycle": str(cycle),
                    "prediction_as_of": f"{cycle}-06-30",
                }
            )
            labels.append(
                {
                    "row_id": row_id,
                    "esg_score": float(cycle - 2000 + company_index),
                    "esg_label_published_at": f"{cycle}-09-01",
                    "esg_authorization_reference": "licensed-fixture",
                    "csa_score": None,
                    "csa_label_published_at": None,
                    "csa_authorization_reference": None,
                }
            )
            features.append({})
    dataset = {
        "snapshot_id": "historical-test",
        "companies": companies,
        "labels": labels,
        "X": features,
    }

    split = build_split_assignments(dataset, "esg", _config(), "historical")
    company_by_id = {row["row_id"]: row for row in companies}
    label_by_id = {row["row_id"]: row for row in labels}

    assert split["mode"] == "historical_forecasting"
    assert split["final_test_cycle"] == "2023"
    assert {
        company_by_id[row_id]["assessment_cycle"]
        for row_id in split["final_test_row_ids"]
    } == {"2023"}
    assert len(split["folds"]) == 2
    for fold in split["folds"]:
        cutoff = date.fromisoformat(fold["cutoff"])
        validation_cycles = {
            company_by_id[row_id]["assessment_cycle"]
            for row_id in fold["validation_row_ids"]
        }
        training_cycles = {
            company_by_id[row_id]["assessment_cycle"]
            for row_id in fold["train_row_ids"]
        }
        assert max(training_cycles) < min(validation_cycles)
        assert all(
            date.fromisoformat(label_by_id[row_id]["esg_label_published_at"]) <= cutoff
            for row_id in fold["train_row_ids"]
        )
        assert all(
            date.fromisoformat(company_by_id[row_id]["prediction_as_of"])
            < date.fromisoformat(label_by_id[row_id]["esg_label_published_at"])
            for row_id in fold["validation_row_ids"]
        )
