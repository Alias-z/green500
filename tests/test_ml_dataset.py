"""Verify dated Green500 ML data contracts with synthetic source records."""

import csv
from copy import deepcopy

from green500.ml.contracts import LabelRecord
from green500.ml.dataset import (
    build_dataset_from_records,
    export_dataset_snapshot,
    load_dataset_snapshot,
    recompute_derived_features,
)
from green500.ml.feature_registry import (
    categorical_feature_names,
    derived_feature_names,
    feature_registry,
    numeric_feature_names,
)
from green500.ml.labels import LABEL_TEMPLATE_COLUMNS, load_authorized_labels


def company(cik="0000000001", industry="Industrials"):
    """Return one synthetic company with join identifiers outside model features."""
    return {
        "company_cik": cik,
        "ticker": "TEST",
        "company_name": "Synthetic Company",
        "industry": industry,
    }


def observation(
    feature_name,
    value,
    *,
    publication_date="2024-04-01",
    year=2023,
    status="reported",
    missing_reason=None,
):
    """Return one synthetic canonical observation with explicit source timing."""
    definition = feature_registry()[feature_name]
    return {
        "company_cik": "0000000001",
        "feature_name": feature_name,
        "value": value,
        "unit": definition["canonical_unit"],
        "reporting_year": year,
        "publication_date": publication_date,
        "processed_at": "2024-04-02T00:00:00+00:00",
        "boundary": "Global operations",
        "status": status,
        "missing_reason": missing_reason
        if value is None
        else None,
        "qualification": None,
        "confidence": 0.9,
        "source_document_id": 1,
        "source_url": "https://example.com/report.pdf",
        "evidence": {"quote": "Synthetic fixture evidence."},
    }


def label(target_name="esg", score=55):
    """Return one synthetic authorized label record."""
    return {
        "company_cik": "0000000001",
        "target_name": target_name,
        "assessment_cycle": "2024-cycle",
        "prediction_as_of": "2024-06-30",
        "score": score,
        "label_published_at": "2024-09-01",
        "source_name": "Synthetic official provider",
        "source_url": "https://example.com/score",
        "source_date": "2024-09-01",
        "source_sha256": "a" * 64,
        "authorization_reference": "Synthetic fixture authorization",
    }


def test_feature_registry_reuses_primitives_and_excludes_join_identifiers():
    """The shared registry has 49 primitives, four ratios, and one category column."""
    registry = feature_registry()
    required_keys = {
        "feature_name",
        "source_JSON_path",
        "data_type",
        "canonical_unit",
        "category_or_context",
        "validation_rules",
        "derived_feature_dependencies",
        "categorical",
        "predictive",
    }
    assert len(registry) == 54
    assert len(numeric_feature_names()) == 53
    assert categorical_feature_names() == ["industry"]
    assert derived_feature_names() == [
        "operating_margin",
        "debt_assets",
        "capex_revenue",
        "emissions_intensity",
    ]
    assert all(set(definition) == required_keys for definition in registry.values())
    assert not {"company_name", "ticker", "company_cik", "source_url"} & set(registry)


def test_cutoff_keeps_zero_and_independent_labels_without_identifier_leakage():
    """Future observations are quarantined, zero survives, and CSA may be absent."""
    observations = [
        observation("financial_revenue_usd", 100.0),
        observation("financial_revenue_usd", 200.0, publication_date="2024-07-01"),
        observation("financial_operating_income_usd", 20.0),
        observation("social_employee_fatalities_count", 0),
    ]
    dataset = build_dataset_from_records([company()], observations, [label()])
    assert len(dataset["X"]) == 1
    features = dataset["X"][0]
    assert features["financial_revenue_usd"] == 100.0
    assert features["social_employee_fatalities_count"] == 0
    assert features["operating_margin"] == 0.2
    assert dataset["labels"][0]["esg_score"] == 55.0
    assert dataset["labels"][0]["csa_score"] is None
    assert not {"row_id", "company_cik", "ticker", "company_name"} & set(features)
    assert any(
        item["reason"] == "The source became available after prediction_as_of."
        for item in dataset["metadata"][0]["quarantined_observations"]
    )


def test_acquisition_fallback_keeps_publication_date_null_and_persists_policy():
    """Delivery mode uses exact document acquisition only as availability evidence."""
    source = observation(
        "env_scope_1_tco2e",
        100.0,
        publication_date=None,
    )
    source.update(
        availability_date="2024-04-02",
        availability_basis="public_document_acquisition_date",
        public_document_acquired_at="2024-04-02T10:30:00+00:00",
        source_document_sha256="b" * 64,
    )

    strict = build_dataset_from_records([company()], [source], [label()])
    delivery = build_dataset_from_records(
        [company()],
        [source],
        [label()],
        "public-document-acquisition-fallback",
    )

    assert strict["X"][0]["env_scope_1_tco2e"] is None
    assert strict["metadata"][0]["features"]["env_scope_1_tco2e"][
        "availability_basis"
    ] == "unavailable"
    delivery_metadata = delivery["metadata"][0]["features"]["env_scope_1_tco2e"]
    assert delivery["X"][0]["env_scope_1_tco2e"] == 100.0
    assert delivery_metadata["publication_date"] is None
    assert delivery_metadata["availability_date"] == "2024-04-02"
    assert delivery_metadata["availability_basis"] == (
        "public_document_acquisition_date"
    )
    assert delivery_metadata["source_document_sha256"] == "b" * 64
    assert delivery["schema"]["availability_policy"] == (
        "public_document_acquisition_fallback"
    )

    future_source = deepcopy(source)
    future_source["availability_date"] = "2024-07-01"
    future_source["public_document_acquired_at"] = "2024-07-01T10:30:00+00:00"
    future = build_dataset_from_records(
        [company()],
        [future_source],
        [label()],
        "public-document-acquisition-fallback",
    )
    assert future["X"][0]["env_scope_1_tco2e"] is None
    assert any(
        item["availability_basis"] == "public_document_acquisition_date"
        and item["reason"] == "The source became available after prediction_as_of."
        for item in future["metadata"][0]["quarantined_observations"]
    )


def test_undated_and_incompatible_observations_remain_missing():
    """Undated values and cross-period ratios are quarantined instead of guessed."""
    undated = observation("financial_total_debt_usd", 25.0, publication_date=None)
    observations = [
        undated,
        observation("financial_revenue_usd", 100.0, year=2023),
        observation("financial_total_capex_usd", 5.0, year=2022),
    ]
    dataset = build_dataset_from_records([company()], observations, [label()])
    features = dataset["X"][0]
    metadata = dataset["metadata"][0]["features"]
    assert features["financial_total_debt_usd"] is None
    assert features["capex_revenue"] is None
    assert "reporting years are incompatible" in metadata["capex_revenue"][
        "missing_reason"
    ]


def test_saved_metadata_preserves_source_missing_semantics_through_quarantine():
    """Date filtering retains extraction and semantic absence as separate statuses."""
    observations = [
        observation(
            "financial_green_transition_capex_usd",
            None,
            publication_date=None,
            status="not_disclosed",
            missing_reason="The company did not disclose this metric.",
        ),
        observation(
            "social_employee_turnover_percent",
            None,
            publication_date=None,
            status="processing",
            missing_reason="The category extraction is still processing.",
        ),
        observation(
            "env_total_energy_mwh",
            None,
            publication_date=None,
            status="completed",
            missing_reason="The validated result has no classified reason.",
        ),
        observation(
            "social_employee_fatalities_count",
            None,
            publication_date=None,
            status="not_applicable",
            missing_reason="The metric does not apply to this boundary.",
        ),
    ]
    dataset = build_dataset_from_records([company()], observations, [label()])
    metadata = dataset["metadata"][0]["features"]
    assert metadata["financial_green_transition_capex_usd"][
        "source_missing_status"
    ] == "not_disclosed"
    assert metadata["social_employee_turnover_percent"][
        "source_missing_status"
    ] == "processing"
    assert metadata["env_total_energy_mwh"]["source_missing_status"] == (
        "unspecified_null"
    )
    assert metadata["social_employee_fatalities_count"][
        "source_missing_status"
    ] == "not_applicable"
    assert metadata["env_scope_1_tco2e"]["source_missing_status"] == "not_extracted"


def test_recompute_derived_features_does_not_mutate_inputs():
    """Scenario recomputation copies observations and checks positive denominators."""
    features = {
        "financial_operating_income_usd": 10.0,
        "financial_revenue_usd": 0.0,
    }
    metadata = {
        name: {
            "canonical_unit": "USD",
            "reporting_year": 2024,
            "publication_date": "2025-01-01",
            "boundary": "Global operations",
            "confidence": 1.0,
        }
        for name in features
    }
    original_features = deepcopy(features)
    original_metadata = deepcopy(metadata)
    updated_features, updated_metadata = recompute_derived_features(features, metadata)
    assert features == original_features and metadata == original_metadata
    assert updated_features["operating_margin"] is None
    assert "denominator must be positive" in updated_metadata["operating_margin"][
        "missing_reason"
    ]


def test_authorized_label_import_requires_exact_template_and_preserves_zero(tmp_path):
    """The label adapter accepts genuine zero and rejects implicit schema changes."""
    path = tmp_path / "labels.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=LABEL_TEMPLATE_COLUMNS)
        writer.writeheader()
        writer.writerow(label(score=0))
    labels = load_authorized_labels(path)
    assert labels == [LabelRecord.model_validate(label(score=0), strict=False)]
    assert labels[0].score == 0

    path.write_text("company_cik,score\n1,10\n")
    try:
        load_authorized_labels(path)
    except ValueError as error:
        assert "exactly match" in str(error)
    else:
        raise AssertionError("A changed label-import schema must fail closed.")


def test_exported_snapshot_reloads_identical_rows(tmp_path):
    """Content-addressed snapshots preserve typed values and aligned row identity."""
    observations = [
        observation("financial_revenue_usd", 100.0),
        observation("financial_operating_income_usd", 20.0),
        observation("climate_sbti_validated", True),
    ]
    dataset = build_dataset_from_records(
        [company()],
        observations,
        [label("esg"), label("csa", 44)],
        "public-document-acquisition-fallback",
    )
    result = export_dataset_snapshot(dataset, tmp_path / "training")
    loaded = load_dataset_snapshot(tmp_path / "training")
    assert result["row_count"] == 1
    assert loaded["companies"][0]["row_id"] == dataset["companies"][0]["row_id"]
    assert loaded["X"] == dataset["X"]
    assert loaded["snapshot_id"] == result["snapshot_id"]
    assert loaded["labels"][0]["esg_score"] == 55.0
    assert loaded["labels"][0]["csa_score"] == 44.0
    assert loaded["manifest"]["availability_policy"] == (
        "public_document_acquisition_fallback"
    )
    assert loaded["metadata"]["availability_policy"] == (
        "public_document_acquisition_fallback"
    )
