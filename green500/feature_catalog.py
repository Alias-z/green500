"""Export fixed predictor columns with evidence and measurement dates kept separate."""

import argparse
import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from green500 import db
from green500.processing.files import save_json
from green500.processing.fixed_report_batch import PROFILES, VERSION, category_profile

FINANCIAL_FIELDS = (
    "revenue", "employees", "total_capex", "green_transition_capex", "operating_income",
    "total_assets", "cash_and_equivalents", "total_debt", "operating_cash_flow",
)
IDENTIFIERS = ("company_cik", "ticker", "company_name")


def feature_schema():
    """Declare stable units and types, independently of observed company coverage."""
    fields = {}
    for field in FINANCIAL_FIELDS:
        key = "financial_" + field + ("_count" if field == "employees" else "_usd")
        fields[key] = {"category": "financial", "unit": "count" if field == "employees" else "USD", "type": "number",
            "description": "Existing annual financial extraction: " + field.replace("_", " ")}
    for category in PROFILES:
        for field, specification in category_profile(category).FIELDS.items():
            if field in fields:
                raise ValueError("Duplicate predictor name: " + field)
            fields[field] = {"category": category, **specification}
    return {"version": VERSION, "identifiers": list(IDENTIFIERS), "fields": fields,
        "missing_values": "JSON null; empty CSV cell. A missing disclosure is never zero.",
        "period_policy": "Current available disclosures; category years are separate metadata. Cross-category years are not assumed aligned.",
        "labels": "Official ESG and CSA scores are excluded from predictor exports."}


def feature_catalog(settings):
    """Read fresh fixed results and unchanged financial results for all current companies."""
    from green500.processed_financial import financial_results
    from green500.processed_reports import report_results

    with db.connect(settings) as connection:
        companies = connection.execute("SELECT cik,name,symbols FROM companies WHERE is_current ORDER BY name,cik").fetchall()
    financial = financial_results(settings)
    reports = report_results(settings)
    schema = feature_schema()
    rows, metadata = [], {}
    for company in companies:
        cik = company["cik"]
        row = {"company_cik": cik, "ticker": company["symbols"][0] if company["symbols"] else cik,
               "company_name": company["name"], **dict.fromkeys(schema["fields"])}
        company_metadata = {}
        detail = financial.get(cik, {})
        data = detail.get("data")
        if data:
            fields = {}
            for field in FINANCIAL_FIELDS:
                key = "financial_" + field + ("_count" if field == "employees" else "_usd")
                metric = data.get(field)
                if metric:
                    eligible = field == "employees" or data["currency"] == "USD"
                    row[key] = metric["value"] if eligible else None
                    fields[key] = {**metric, "export_reason": None if eligible else "Non-USD source retained in original financial JSON; no exchange-rate conversion."}
            company_metadata["financial"] = {"reporting_year": data["fiscal_year"], "currency": data["currency"],
                "source_document_id": detail.get("source_document_id"), "processed_at": detail.get("processed_at"),
                "fields": fields, "evidence": detail.get("evidence", {})}
        for category in PROFILES:
            detail = reports.get(cik, {}).get(category, {})
            data = detail.get("data")
            if not data or "values" not in data or "metadata" not in data:
                company_metadata[category] = {"status": detail.get("processing", {}).get("status", "not_started"),
                                              "reporting_year": None}
                continue
            parsed = category_profile(category).MODEL.model_validate(data).model_dump(mode="json")
            row.update(parsed["values"])
            company_metadata[category] = {"status": "completed", "reporting_year": parsed["reporting_year"],
                "boundary": parsed["boundary"], "limitations": parsed["limitations"], "fields": parsed["metadata"],
                "source_document_id": detail.get("source_document_id"), "processed_at": detail.get("processed_at"),
                "evidence": detail.get("evidence", {})}
        rows.append(row)
        metadata[cik] = company_metadata
    return {"generated_at": datetime.now(UTC).isoformat(), "schema": schema, "rows": rows, "metadata": metadata}


def features_csv(catalog):
    """Render fixed numeric/boolean predictors; booleans use 1/0 and missing cells are empty."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=[*IDENTIFIERS, *catalog["schema"]["fields"]])
    writer.writeheader()
    for row in catalog["rows"]:
        writer.writerow({key: int(value) if isinstance(value, bool) else value for key, value in row.items()})
    return stream.getvalue()


def export_features(settings, directory):
    """Write a current snapshot without modifying any extraction result."""
    catalog = feature_catalog(settings)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    save_json(directory / "schema.json", catalog["schema"])
    save_json(directory / "features.json", {"generated_at": catalog["generated_at"], "rows": catalog["rows"]})
    save_json(directory / "metadata.json", {"generated_at": catalog["generated_at"], "companies": catalog["metadata"]})
    temporary = directory / "features.csv.tmp"
    temporary.write_text(features_csv(catalog))
    temporary.replace(directory / "features.csv")
    return {"companies": len(catalog["rows"]), "features": len(catalog["schema"]["fields"]), "directory": str(directory)}


def main():
    from green500.config import load_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    settings = load_settings()
    print(json.dumps(export_features(settings, args.output_dir or settings.data_dir / "ml")))


if __name__ == "__main__":
    main()
