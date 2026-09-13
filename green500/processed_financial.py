"""Read-only financial processing status and validated results for the viewer."""

import hashlib
import json
import re
import threading
from urllib.parse import urljoin

from lxml import html

from green500 import db
from green500.processing.financial_schema import VERDEXFinancialData

_LOCK = threading.Lock()
_CACHE = {"signature": None, "records": []}
_SEC_ARCHIVE_URL = re.compile(
    r"^https://www\.sec\.gov/Archives/edgar/data/"
    r"(?P<cik>\d+)/(?P<accession>\d+)/(?P<filename>[^/?#]+)$"
)


def _load_json(path):
    """Read a bounded JSON result without following an external symlink."""
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError("Financial result file is invalid.")
    return json.loads(path.read_bytes())


def _has_required_checks(receipt, checks):
    """Accept local source checks and require model review only when requested."""
    if not isinstance(checks, dict) or checks.get("status") != "passed":
        return False
    if checks.get("source_checks") is not True:
        return False
    if receipt.get("review_with_model") is False:
        return checks.get("reviewed_by_model") is False and checks.get("semantic_checks") == []
    judgments = checks.get("semantic_checks")
    return (
        isinstance(judgments, list)
        and len(judgments) == 9
        and all(isinstance(item, dict) and item.get("accepted") is True for item in judgments)
        and checks.get("reviewed_by_model", True) is True
    )


def _records(settings):
    """Cache only production receipts; experimental model directories stay excluded."""
    if not getattr(settings, "data_dir", None):
        return []
    base = settings.data_dir / "financial_extractions"
    paths = []
    for root in (base, base / "volcengine_bulk"):
        for path in root.glob("*/*/receipt.json"):
            if path.resolve().is_relative_to(base.resolve()):
                paths.append(path)
    signature = []
    for path in sorted(paths):
        for name in (
            "receipt.json",
            "financial.json",
            "validation.json",
            "evidence.json",
            "source_manifest.json",
        ):
            item = path.parent / name
            try:
                stat = item.stat()
                signature.append((str(item), stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((str(item), None, None))
    with _LOCK:
        if _CACHE["signature"] == signature:
            return _CACHE["records"]
        records = []
        for path in paths:
            try:
                receipt = _load_json(path)
                if not isinstance(receipt, dict):
                    continue
                if not isinstance(receipt.get("document_id"), int):
                    continue
                result = None
                evidence = None
                if receipt.get("status") == "succeeded":
                    if (path.parent / "financial.json").is_symlink():
                        raise ValueError("Financial result file is invalid.")
                    if (path.parent / "financial.json").stat().st_size > 2_000_000:
                        raise ValueError("Financial result file is too large.")
                    raw = (path.parent / "financial.json").read_bytes()
                    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != receipt.get(
                        "financial_sha256"
                    ):
                        raise ValueError("Financial result integrity check failed.")
                    result = VERDEXFinancialData.model_validate_json(raw).model_dump(mode="json")
                    checks = _load_json(path.parent / "validation.json")
                    if not _has_required_checks(receipt, checks):
                        raise ValueError("Financial result is missing its required local checks or requested review.")
                    evidence = _load_json(path.parent / "evidence.json")
                    if result["company_id"] != receipt.get("company_id"):
                        raise ValueError("Financial result company mismatch.")
                source_manifest_path = path.parent / "source_manifest.json"
                source_manifest = (
                    _load_json(source_manifest_path)
                    if source_manifest_path.exists()
                    else {}
                )
                records.append(
                    {
                        "receipt": receipt,
                        "result": result,
                        "evidence": evidence,
                        "source_manifest": source_manifest,
                    }
                )
            except (OSError, ValueError, KeyError, TypeError):
                try:
                    receipt = _load_json(path)
                    if not isinstance(receipt, dict):
                        continue
                    receipt["status"] = "failed"
                    records.append({"receipt": receipt, "result": None, "evidence": None})
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        _CACHE.update(signature=signature, records=records)
        return records


def _public_evidence(evidence):
    """Expose source references and quotations without storage paths or model requests."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("metrics",{}),dict):
        return {}
    result = {}
    for name, value in evidence.get("metrics", {}).items():
        if not isinstance(value, dict):
            continue
        allowed = {
            k: value[k]
            for k in ("block_id", "row_id", "quote", "source_scale", "formula", "basis")
            if k in value
        }
        if isinstance(value.get("operands"), list):
            allowed["operands"] = [
                {
                    k: operand[k]
                    for k in ("block_id", "row_id", "quote", "source_scale", "component_value")
                    if k in operand
                }
                for operand in value["operands"]
                if isinstance(operand, dict)
            ]
        result[name] = allowed
    return result


def _content_addressed_body(settings, sha256):
    """Read and verify one registered content-addressed source object."""
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise ValueError("Source SHA-256 is invalid.")
    path = settings.data_dir / "objects" / sha256[:2] / sha256
    if path.is_symlink():
        raise ValueError("Source object must not be a symlink.")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != sha256:
        raise ValueError("Source object failed hash verification.")
    return body


def _is_verified_annual_attachment(record, document, documents, settings):
    """Accept only an attachment linked by its registered same-accession wrapper."""
    receipt = record["receipt"]
    source_manifest = record.get("source_manifest")
    if not isinstance(source_manifest, dict):
        return False
    wrapper = source_manifest.get("annual_report_wrapper")
    if not isinstance(wrapper, dict) or wrapper != receipt.get("annual_report_wrapper"):
        return False
    required = {
        "basis",
        "fiscal_year",
        "wrapper_document_id",
        "wrapper_sha256",
        "wrapper_url",
        "attachment_document_id",
        "attachment_sha256",
        "attachment_url",
        "sec_accession",
        "exact_wrapper_link",
    }
    if not required.issubset(wrapper) or wrapper["basis"] != "same_accession_sec_annual_wrapper":
        return False
    if (
        wrapper["attachment_document_id"] != document["id"]
        or wrapper["attachment_sha256"] != document["sha256"]
        or wrapper["attachment_sha256"] != receipt.get("source_sha256")
        or source_manifest.get("source_sha256") != document["sha256"]
        or wrapper["attachment_url"] != receipt.get("source_url")
        or source_manifest.get("source_url") != wrapper["attachment_url"]
        or wrapper["attachment_url"] not in {document["url"], document["final_url"]}
        or wrapper["fiscal_year"] != receipt.get("fiscal_year")
        or wrapper["fiscal_year"] != source_manifest.get("fiscal_year")
    ):
        return False
    wrapper_document = documents.get(wrapper["wrapper_document_id"])
    if not wrapper_document or (
        wrapper_document["company_cik"] != document["company_cik"]
        or wrapper_document["sha256"] != wrapper["wrapper_sha256"]
        or wrapper["wrapper_url"]
        not in {wrapper_document["url"], wrapper_document["final_url"]}
    ):
        return False
    wrapper_identity = _SEC_ARCHIVE_URL.fullmatch(wrapper["wrapper_url"])
    attachment_identity = _SEC_ARCHIVE_URL.fullmatch(wrapper["attachment_url"])
    if not wrapper_identity or not attachment_identity or (
        int(wrapper_identity["cik"]) != int(document["company_cik"])
        or wrapper_identity["cik"] != attachment_identity["cik"]
        or wrapper_identity["accession"] != attachment_identity["accession"]
        or wrapper_identity["accession"] != wrapper["sec_accession"]
    ):
        return False
    exact_link = wrapper["exact_wrapper_link"]
    if not isinstance(exact_link, dict) or exact_link.get("resolved_url") != wrapper[
        "attachment_url"
    ]:
        return False
    try:
        _content_addressed_body(settings, document["sha256"])
        wrapper_body = _content_addressed_body(settings, wrapper["wrapper_sha256"])
        links = [
            link
            for link in html.fromstring(wrapper_body).xpath("//a[@href]")
            if urljoin(wrapper["wrapper_url"], link.get("href"))
            == wrapper["attachment_url"]
        ]
    except (OSError, TypeError, ValueError):
        return False
    return len(links) == 1 and (
        links[0].get("href") == exact_link.get("href")
        and " ".join(" ".join(links[0].itertext()).split())
        == exact_link.get("link_text")
    )


def financial_results(settings):
    """Join processing receipts to registered annual filings by document and source hash."""
    records = _records(settings)
    ids = list(
        {
            r["receipt"].get("document_id")
            for r in records
            if isinstance(r["receipt"].get("document_id"), int)
        }
    )
    ids.extend(
        wrapper["wrapper_document_id"]
        for record in records
        if isinstance(record.get("source_manifest"), dict)
        and isinstance(
            wrapper := record["source_manifest"].get("annual_report_wrapper"), dict
        )
        and isinstance(wrapper.get("wrapper_document_id"), int)
    )
    ids = list(set(ids))
    if not ids:
        return {}
    with db.connect(settings) as connection:
        documents = connection.execute(
            "SELECT d.id,d.company_cik,d.sha256,d.title,d.url,d.final_url,c.symbols FROM documents d JOIN companies c ON c.cik=d.company_cik WHERE d.id=ANY(%s) AND d.kind='report' AND c.is_current",
            (ids,),
        ).fetchall()
    documents = {document["id"]: document for document in documents}
    groups = {}
    for record in records:
        receipt = record["receipt"]
        document = documents.get(receipt.get("document_id"))
        if not document or document["sha256"] != receipt.get("source_sha256"):
            continue
        if re.search(r"\bSEC (10-K|20-F|40-F) filed\b", document["title"]) is None and not (
            _is_verified_annual_attachment(record, document, documents, settings)
        ):
            continue
        if record["result"] is not None:
            data = record["result"]
            if data["company_id"] not in [document["company_cik"],*document["symbols"]]:
                continue
            if any(isinstance(value,dict) and value.get("source_url") not in {None,document["url"],document["final_url"]} for value in data.values()):
                continue
        groups.setdefault(document["company_cik"], []).append((record, document))
    result = {}
    for cik, items in groups.items():
        items.sort(key=lambda pair: str(pair[0]["receipt"].get("started_at") or ""), reverse=True)
        latest, latest_document = items[0]
        successful = next((pair for pair in items if pair[0]["result"] is not None), None)
        receipt = latest["receipt"]
        status = (
            "completed"
            if latest["result"] is not None
            else "processing"
            if receipt.get("status") == "running"
            else "needs_review"
        )
        summary = {
            "status": status,
            "has_result": successful is not None,
            "document_id": latest_document["id"],
            "model": receipt.get("model"),
            "fiscal_year": receipt.get("fiscal_year"),
            "updated_at": receipt.get("finished_at") or receipt.get("started_at"),
        }
        detail = {"processing": summary, "data": None, "evidence": {}, "source_document_id": None}
        if successful:
            saved, document = successful
            detail.update(
                data=saved["result"],
                evidence=_public_evidence(saved["evidence"]),
                source_document_id=document["id"],
                model=saved["receipt"].get("model"),
                processed_at=saved["receipt"].get("finished_at"),
                prompt_version=saved["receipt"].get("prompt_version"),
            )
        result[cik] = detail
    return result


def processing_summary(settings):
    """Return one small processing status object for each company with an attempt."""
    return {cik: value["processing"] for cik, value in financial_results(settings).items()}
