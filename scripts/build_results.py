"""Build the website's results.json from company results and the materiality CSV."""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build():
    by_ticker = {}
    for path in sorted((ROOT / "data/report_examples").glob("*/results.json")):
        result = json.loads(path.read_text())
        ticker = result["company"]["ticker"]
        if ticker in by_ticker:
            raise ValueError(f"Duplicate results for {ticker}")
        by_ticker[ticker] = result
    with (ROOT / "data/reference/company_materiality.csv").open() as stream:
        reader = csv.DictReader(stream)
        topics = reader.fieldnames[4:19]
        companies = []
        for row in reader:
            ticker = row["Ticker"]
            result = by_ticker.pop(ticker, {})
            companies.append({
                **result,
                "company": {**result.get("company", {}), "ticker": ticker,
                            "name": result.get("company", {}).get("name", row["Company"]),
                            "sector": row["GICS Sector"], "sub_industry": row["GICS Sub-Industry"]},
                "materiality": {"topics": {key: int(row[key]) for key in topics},
                                "average": float(row["Avg Materiality"]),
                                "high_priority_topics": row["High-Priority Topics (score 3)"],
                                "method": row["Profile Method"]},
                "environment": result.get("environment", {"status": "not_extracted"}),
            })
    if by_ticker:
        raise ValueError(f"Results without materiality reference: {list(by_ticker)}")
    output = {"schema_version": 1, "materiality_source": "data/reference/company_materiality.csv",
              "materiality_note": "Topic relevance, not measured sustainability performance.",
              "companies": companies}
    (ROOT / "results.json").write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    return output


if __name__ == "__main__":
    result = build()
    print(f"Wrote results.json with {len(result['companies'])} securities")
