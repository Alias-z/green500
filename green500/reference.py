"""Attach source-backed GICS classifications without materiality ratings or scores."""

from __future__ import annotations

import copy
import csv
import hashlib
import threading
from pathlib import Path
from urllib.parse import urlsplit

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "data/reference"
SOURCE_COMMIT = "a8794f6695c080a6e99e69f7597ffcab5ec25aec"
CLASSIFICATION_FILE = "company_materiality.csv"
METADATA_FILES = (CLASSIFICATION_FILE, "sources.csv", "workbook_readme.csv")
REQUIRED_COLUMNS = ("Ticker", "Company", "GICS Sector", "GICS Sub-Industry")

_CACHE_LOCK = threading.Lock()
_CACHE_SIGNATURE = None
_CACHE_BUNDLE = None


def _table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read one CSV with unique named columns and no overflow values."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if not headers or len(headers) != len(set(headers)) or None in headers:
            raise ValueError(f"{path.name} requires unique named columns.")
        rows = list(reader)
    if any(None in row for row in rows):
        raise ValueError(f"{path.name} contains a row with extra columns.")
    return headers, rows


def _load_bundle(directory: Path) -> dict:
    """Validate and load only ticker, sector and sub-industry source fields."""
    headers, rows = _table(directory / CLASSIFICATION_FILE)
    if tuple(headers[:4]) != REQUIRED_COLUMNS:
        raise ValueError(
            "The classification CSV must start with ticker, company, sector and sub-industry."
        )
    classifications = {}
    for row in rows:
        ticker = row["Ticker"].strip()
        sector = row["GICS Sector"].strip()
        sub_industry = row["GICS Sub-Industry"].strip()
        if not ticker or ticker in classifications:
            raise ValueError("Classification tickers must be present and unique.")
        if not sector or not sub_industry:
            raise ValueError(f"{ticker} requires a GICS sector and sub-industry.")
        classifications[ticker] = {
            "ticker": ticker,
            "sector": sector,
            "sub_industry": sub_industry,
        }
    if not 400 <= len(classifications) <= 800:
        raise ValueError("Classification security count is outside the expected range.")

    with (directory / "workbook_readme.csv").open(
        newline="", encoding="utf-8-sig"
    ) as handle:
        readme_rows = list(csv.reader(handle))
    if not readme_rows or any(len(row) != 2 or not row[0] for row in readme_rows):
        raise ValueError("workbook_readme.csv must contain two-column key/value rows.")
    readme = dict(readme_rows[1:])
    if "As of" not in readme or not readme["As of"]:
        raise ValueError("workbook_readme.csv is missing its as-of date.")

    source_headers, source_rows = _table(directory / "sources.csv")
    if source_headers != ["Source", "Publisher/Host", "URL", "Use"]:
        raise ValueError("sources.csv has unexpected columns.")
    sources = []
    for row in source_rows:
        if "GICS" not in row["Use"] and "GICS" not in row["Source"]:
            continue
        parsed = urlsplit(row["URL"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                "Every classification source requires a public HTTP(S) URL."
            )
        sources.append(
            {
                "name": row["Source"],
                "publisher": row["Publisher/Host"],
                "url": row["URL"],
                "use": row["Use"],
            }
        )
    if len(sources) != 2:
        raise ValueError("The reference requires constituent and GICS public sources.")
    metadata = {
        "title": "S&P 500 GICS sector and sub-industry classifications",
        "as_of": readme["As of"],
        "source_commit": SOURCE_COMMIT,
        "sources": sources,
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
            }
            for name in METADATA_FILES
        ],
        "security_count": len(classifications),
    }
    return {"metadata": metadata, "classifications": classifications}


def _bundle() -> dict:
    """Reuse classifications until any classification metadata file mtime changes."""
    global _CACHE_BUNDLE, _CACHE_SIGNATURE
    directory = REFERENCE_DIR.resolve()
    signature = (str(directory),) + tuple(
        (name, (directory / name).stat().st_mtime_ns) for name in METADATA_FILES
    )
    with _CACHE_LOCK:
        if signature != _CACHE_SIGNATURE:
            _CACHE_BUNDLE = _load_bundle(directory)
            _CACHE_SIGNATURE = signature
        return _CACHE_BUNDLE


def load_reference() -> dict:
    """Return classification-source metadata without ratings or topic priorities."""
    return copy.deepcopy(_bundle()["metadata"])


def attach_company_reference(companies: list[dict]) -> None:
    """Attach exact-symbol classifications and preserve disagreeing share classes."""
    classifications = _bundle()["classifications"]
    for company in companies:
        matches = [
            classifications[symbol]
            for symbol in (company.get("symbols") or [])
            if symbol in classifications
        ]
        classification = {
            "match_status": "unmatched",
            "source_tickers": [],
            "sector": None,
            "sub_industry": None,
            "source_commit": SOURCE_COMMIT,
        }
        if matches:
            classification["source_tickers"] = [match["ticker"] for match in matches]
            variants = {(match["sector"], match["sub_industry"]) for match in matches}
            if len(variants) == 1:
                classification.update(
                    match_status="matched",
                    sector=matches[0]["sector"],
                    sub_industry=matches[0]["sub_industry"],
                )
                company["sector"] = matches[0]["sector"]
                company["sub_industry"] = matches[0]["sub_industry"]
            else:
                classification["match_status"] = "ambiguous"
                classification["variants"] = copy.deepcopy(matches)
                company["sub_industry"] = None
        else:
            company["sub_industry"] = None
        company["classification"] = classification
