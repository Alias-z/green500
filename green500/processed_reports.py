"""Read fresh fixed-schema report extractions for the authenticated viewer."""

import hashlib
import json
import re
import threading

from green500 import db
from green500.processing.fixed_report_batch import PROFILES, category_profile
from green500.processing.fixed_report_batch import VERSION as TEXT_VERSION

_LOCK = threading.Lock()
_CACHE = {"signature": None, "records": []}
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _json(path):
    """Read one bounded result file without following a symlink."""
    if path.is_symlink() or path.stat().st_size > 3_000_000:
        raise ValueError("Invalid structured extraction file.")
    return json.loads(path.read_bytes())


def _receipt_paths(base):
    """Find the fixed text extractor's provider/company/fingerprint layout."""
    paths = []
    for category in PROFILES:
        root = base / category
        paths.extend(root.glob("text/*/*/*/receipt.json"))
    return sorted(paths)


def _records(settings):
    """Cache fixed outputs while retaining current processing receipts."""
    if not getattr(settings, "data_dir", None):
        return []
    base = settings.data_dir / "structured_extractions"
    paths = _receipt_paths(base)
    signature = []
    for path in paths:
        for name in ("receipt.json", "data.json", "validation.json", "evidence.json"):
            item = path.parent / name
            try:
                stat = item.stat()
                signature.append((str(item), stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((str(item), None, None))
    with _LOCK:
        if signature == _CACHE["signature"]:
            return _CACHE["records"]
        records = []
        resolved_base = base.resolve()
        for path in paths:
            receipt = None
            try:
                if not path.resolve().is_relative_to(resolved_base):
                    continue
                receipt = _json(path)
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("category") not in PROFILES
                    or receipt.get("version") != TEXT_VERSION
                    or receipt.get("fresh_original_extraction") is not True
                    or not isinstance(receipt.get("document_id"), int)
                    or not re.fullmatch(r"[0-9]{10}", receipt.get("company_cik", ""))
                    or not _SHA256.fullmatch(receipt.get("source_sha256", ""))
                ):
                    continue
                data, evidence = None, {}
                if receipt.get("status") == "succeeded":
                    data_path = path.parent / "data.json"
                    raw = data_path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != receipt.get("output_sha256"):
                        raise ValueError("Structured extraction content changed.")
                    profile = category_profile(receipt["category"])
                    data = profile.MODEL.model_validate(json.loads(raw)).model_dump(
                        mode="json"
                    )
                    if data["company"]["ticker"] != receipt.get("company_id"):
                        raise ValueError(
                            "Structured extraction company does not match its receipt."
                        )
                    checks = _json(path.parent / "validation.json")
                    source_check = checks.get("source_quote_checks") is True
                    if (
                        checks.get("status") != "passed"
                        or checks.get("schema_checks") is not True
                        or checks.get("source_identity_checks") is not True
                        or not source_check
                    ):
                        raise ValueError(
                            "Structured extraction did not pass its local checks."
                        )
                    evidence = _json(path.parent / "evidence.json")
                records.append({"receipt": receipt, "data": data, "evidence": evidence})
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                if isinstance(receipt, dict) and receipt.get("category") in PROFILES:
                    records.append(
                        {
                            "receipt": {**receipt, "status": "failed"},
                            "data": None,
                            "evidence": {},
                        }
                    )
        _CACHE.update(signature=signature, records=records)
        return records


def _definitions(category):
    """Return public field descriptions without executable schema details."""
    return {
        key: {
            "description": specification["description"],
            "unit": specification.get("unit"),
            "type": specification["type"],
        }
        for key, specification in category_profile(category).FIELDS.items()
    }


def report_results(settings):
    """Join fixed results to the exact original and current company identity."""
    records = _records(settings)
    ids = sorted(
        {
            record["receipt"].get("document_id")
            for record in records
            if isinstance(record["receipt"].get("document_id"), int)
        }
    )
    if not ids:
        return {}
    with db.connect(settings) as connection:
        originals = connection.execute(
            """SELECT d.id,d.company_cik,d.sha256,d.url,d.final_url,c.name,c.symbols
            FROM documents d JOIN companies c ON c.cik=d.company_cik
            WHERE d.id=ANY(%s) AND c.is_current""",
            (ids,),
        ).fetchall()
    originals = {document["id"]: document for document in originals}
    grouped = {}
    for record in records:
        receipt = record["receipt"]
        original = originals.get(receipt.get("document_id"))
        if not original or (
            receipt.get("company_cik"),
            receipt.get("source_sha256"),
        ) != (original["company_cik"], original["sha256"]):
            continue
        if record["data"] is not None:
            data = record["data"]
            expected_ticker = (
                original["symbols"][0]
                if original["symbols"]
                else original["company_cik"]
            )
            if data["company"] != {
                "name": original["name"],
                "ticker": expected_ticker,
            }:
                continue
            evidence = record["evidence"]
            if (
                evidence.get("source_sha256") != original["sha256"]
                or evidence.get("source_url")
                not in {original["url"], original["final_url"]}
            ):
                continue
        grouped.setdefault(
            (original["company_cik"], receipt["category"]), []
        ).append(record)
    results = {}
    for (cik, category), items in grouped.items():
        items.sort(
            key=lambda item: str(item["receipt"].get("started_at", "")), reverse=True
        )
        latest = items[0]
        saved = next((item for item in items if item["data"] is not None), None)
        receipt = latest["receipt"]
        status = (
            "completed"
            if latest["data"] is not None
            else "processing"
            if receipt.get("status") == "running"
            else "completed"
            if saved is not None
            else "needs_review"
        )
        detail = {
            "processing": {
                "status": status,
                "has_result": saved is not None,
                "model": receipt.get("model"),
                "document_id": receipt.get("document_id"),
                "updated_at": receipt.get("finished_at")
                or receipt.get("started_at"),
                "populated_fields": (
                    saved["receipt"].get("populated_fields") if saved else None
                ),
            },
            "data": saved["data"] if saved else None,
            "evidence": saved["evidence"] if saved else {},
            "field_definitions": _definitions(category),
            "source_document_id": saved["receipt"]["document_id"] if saved else None,
            "model": saved["receipt"].get("model") if saved else None,
            "processed_at": saved["receipt"].get("finished_at") if saved else None,
        }
        results.setdefault(cik, {})[category] = detail
    return results


def processing_summary(settings):
    """Return compact fixed-category statuses for company rows."""
    return {
        cik: {
            category: result["processing"]
            for category, result in categories.items()
        }
        for cik, categories in report_results(settings).items()
    }
