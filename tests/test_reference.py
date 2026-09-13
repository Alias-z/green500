"""Verify exact-ticker GICS classification attachment without rating exposure."""

import csv
import shutil

import pytest

from green500 import reference


def copy_reference_files(destination):
    """Copy only the three files consumed by the classification loader."""
    for name in reference.METADATA_FILES:
        shutil.copy(reference.REFERENCE_DIR / name, destination / name)


def rewrite_classification(destination, change):
    """Apply one test-only row change while preserving the source columns."""
    path = destination / reference.CLASSIFICATION_FILE
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames
        rows = list(reader)
    for row in rows:
        change(row)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def test_reference_metadata_and_aapl_use_only_gics_fields():
    """AAPL receives source classifications with no materiality or scoring fields."""
    metadata = reference.load_reference()
    companies = [
        {"symbols": ["AAPL"], "sector": "Unverified"},
        {"symbols": ["NOT-IN-SOURCE"], "sector": "Existing sector"},
    ]
    reference.attach_company_reference(companies)

    assert metadata["security_count"] == 503
    assert metadata["source_commit"] == reference.SOURCE_COMMIT
    assert set(metadata) == {
        "title",
        "as_of",
        "source_commit",
        "sources",
        "files",
        "security_count",
    }
    assert companies[0]["sector"] == "Information Technology"
    assert companies[0]["sub_industry"] == (
        "Technology Hardware, Storage & Peripherals"
    )
    assert companies[0]["classification"] == {
        "match_status": "matched",
        "source_tickers": ["AAPL"],
        "sector": "Information Technology",
        "sub_industry": "Technology Hardware, Storage & Peripherals",
        "source_commit": reference.SOURCE_COMMIT,
    }
    assert companies[1]["sector"] == "Existing sector"
    assert companies[1]["sub_industry"] is None
    assert companies[1]["classification"]["match_status"] == "unmatched"
    forbidden = {"ratings", "average_materiality", "high_priority_topics", "score"}
    assert forbidden.isdisjoint(metadata)
    assert forbidden.isdisjoint(companies[0]["classification"])


def test_all_503_securities_attach_to_500_companies_with_consistent_share_classes():
    """The three known dual-share companies collapse without classification conflict."""
    path = reference.REFERENCE_DIR / reference.CLASSIFICATION_FILE
    with path.open(newline="", encoding="utf-8-sig") as handle:
        tickers = [row["Ticker"] for row in csv.DictReader(handle)]
    share_class_groups = (("GOOG", "GOOGL"), ("FOX", "FOXA"), ("NWS", "NWSA"))
    grouped_tickers = {ticker for group in share_class_groups for ticker in group}
    companies = [
        {"symbols": [ticker], "sector": "Unverified"}
        for ticker in tickers
        if ticker not in grouped_tickers
    ]
    companies.extend(
        {"symbols": list(group), "sector": "Unverified"} for group in share_class_groups
    )

    reference.attach_company_reference(companies)

    assert len(tickers) == 503
    assert len(companies) == 500
    assert all(
        company["classification"]["match_status"] == "matched" for company in companies
    )
    assert (
        sum(len(company["classification"]["source_tickers"]) for company in companies)
        == 503
    )
    assert all(company["sector"] and company["sub_industry"] for company in companies)


def test_disagreeing_share_classes_are_ambiguous_and_preserve_variants(
    tmp_path, monkeypatch
):
    """Conflicting source classes remain visible and do not overwrite company sector."""
    copy_reference_files(tmp_path)

    def change(row):
        if row["Ticker"] == "GOOGL":
            row["GICS Sub-Industry"] = "Conflicting test sub-industry"

    rewrite_classification(tmp_path, change)
    monkeypatch.setattr(reference, "REFERENCE_DIR", tmp_path)
    companies = [{"symbols": ["GOOG", "GOOGL"], "sector": "Existing sector"}]

    reference.attach_company_reference(companies)

    classification = companies[0]["classification"]
    assert classification["match_status"] == "ambiguous"
    assert classification["source_tickers"] == ["GOOG", "GOOGL"]
    assert {variant["sub_industry"] for variant in classification["variants"]} == {
        "Interactive Media & Services",
        "Conflicting test sub-industry",
    }
    assert companies[0]["sector"] == "Existing sector"
    assert companies[0]["sub_industry"] is None


def test_malformed_classification_is_rejected(tmp_path, monkeypatch):
    """A blank source classification cannot become trusted company metadata."""
    copy_reference_files(tmp_path)

    def change(row):
        if row["Ticker"] == "AAPL":
            row["GICS Sector"] = ""

    rewrite_classification(tmp_path, change)
    monkeypatch.setattr(reference, "REFERENCE_DIR", tmp_path)

    with pytest.raises(ValueError, match="requires a GICS sector"):
        reference.load_reference()
