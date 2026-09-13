"""Extract fixed environmental fields from at most two report page images."""

import asyncio
import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from green500.processing.chat_completion import completion_text
from green500.processing.environment_features import FIELDS, MODEL
from green500.processing.feature_units import convert
from green500.processing.files import (
    completion_client_identity,
    create_attempt_directory,
    publish_attempt,
    require_safe_path_segment,
    retained_unknown_receipt,
    reusable_successful_receipt,
    save_json,
)
from green500.processing.fixed_report_batch import _parse_fixed_raw_number
from green500.processing.report_page_images import (
    VERSION as IMAGE_INPUT_VERSION,
)
from green500.processing.report_page_images import (
    extraction_messages,
    request_summary,
    validate_company_pages,
)
from green500.processing.source_quotes import normalize_source_text

VERSION = "image-page-environment-v2"
QUALITY_GATE_VERSION = "environment-image-complaint-five-v1"


def _parse_json(text):
    """Parse one complete JSON object, allowing one ordinary JSON code fence."""
    value = text.strip()
    match = re.fullmatch(
        r"```(?:json)?\s*\n?(.*?)\n?```", value, re.DOTALL | re.IGNORECASE
    )
    if match:
        value = match.group(1).strip()
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("Image extraction must return one JSON object.")
    return parsed


def _normalize_transport_variants(parsed):
    """Normalize two harmless model spellings before enforcing the fixed schema."""
    result = json.loads(json.dumps(parsed))
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return result
    confidence_values = {"low": 0.6, "medium": 0.8, "high": 0.95}
    for field_metadata in metadata.values():
        if not isinstance(field_metadata, dict):
            continue
        confidence = field_metadata.get("confidence")
        if (
            "confidence" not in field_metadata
            and "numeric_confidence" in field_metadata
        ):
            field_metadata["confidence"] = field_metadata.pop("numeric_confidence")
        status = field_metadata.get("status")
        if status == "extracted":
            field_metadata["status"] = "reported"
        elif status == "missing":
            field_metadata["status"] = "not_disclosed"
        confidence = field_metadata.get("confidence")
        if isinstance(confidence, str) and confidence.casefold() in confidence_values:
            field_metadata["confidence"] = confidence_values[confidence.casefold()]
        evidence = field_metadata.get("evidence")
        if (
            isinstance(evidence, dict)
            and isinstance(evidence.get("block_id"), str)
            and evidence["block_id"].isdigit()
        ):
            evidence["block_id"] = int(evidence["block_id"])
        if (
            isinstance(evidence, dict)
            and "quote" not in evidence
            and isinstance(evidence.get("verbatim"), str)
        ):
            evidence["quote"] = evidence.pop("verbatim")
    return result


def _validate_field(field, value, metadata, allowed_pages, reporting_year):
    """Validate numerical conversion and page citation without re-running OCR."""
    if metadata.get("reporting_year") != reporting_year:
        raise ValueError(
            field + ": cited year differs from the requested reporting year."
        )
    evidence = metadata.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("block_id") not in allowed_pages:
        raise ValueError(
            field + ": evidence does not identify one supplied page image."
        )
    quote = str(evidence.get("quote") or "")
    raw_value = str(evidence.get("raw_value") or "")
    if not raw_value or normalize_source_text(raw_value).replace(
        ",", ""
    ) not in normalize_source_text(quote).replace(",", ""):
        raise ValueError(field + ": raw value is absent from the visible-row quote.")
    raw_number = _parse_fixed_raw_number(raw_value)
    converted = convert(raw_number, evidence.get("source_unit"), FIELDS[field]["unit"])
    scale_factor = evidence.get("scale_factor")
    if converted is None or not math.isclose(
        converted, value, rel_tol=0.00001, abs_tol=0.000001
    ):
        raise ValueError(field + ": source unit does not produce the canonical value.")
    if not isinstance(scale_factor, (int, float)) or not math.isclose(
        raw_number * scale_factor, value, rel_tol=0.00001, abs_tol=0.000001
    ):
        raise ValueError(
            field + ": conversion scale disagrees with the canonical value."
        )
    context = normalize_source_text(
        " ".join([quote, str(metadata.get("qualification") or "")])
    )
    required_terms = {
        "env_scope_1_tco2e": ("scope 1",),
        "env_scope_2_location_based_tco2e": ("scope 2", "location"),
        "env_scope_2_market_based_tco2e": ("scope 2", "market"),
        "env_scope_3_total_tco2e": ("scope 3",),
        "env_total_energy_mwh": ("energy",),
        "env_renewable_electricity_percent": ("renewable", "electric"),
        "env_water_withdrawal_m3": ("water", "withdraw"),
        "env_total_waste_tonnes": ("waste",),
        "env_waste_recycled_percent": ("recycl",),
    }
    if any(term not in context for term in required_terms.get(field, ())):
        raise ValueError(
            field + ": evidence quote does not state the requested metric."
        )
    if field == "env_scope_3_total_tco2e" and re.search(
        r"categor(?:y|ies)\s*[\d,]", context
    ):
        raise ValueError(
            field + ": selected Scope 3 categories are not a complete inventory."
        )
    if field == "env_total_ghg_tco2e" and not any(
        term in context
        for term in (
            "overall",
            "total ghg",
            "total greenhouse",
            "carbon emissions",
            "scope 1, 2, and 3 emissions",
        )
    ):
        raise ValueError(
            field + ": evidence quote does not state an overall greenhouse-gas total."
        )
    if (
        field == "env_total_energy_mwh"
        and "office energy" in context
        and "total energy" not in context
    ):
        raise ValueError(field + ": office energy is not total company energy.")


def validate_result(text, company, expected_values=None):
    """Validate the closed schema, source pages and optional independent sample truth."""
    parsed = _normalize_transport_variants(_parse_json(text))
    for field_metadata in (parsed.get("metadata") or {}).values():
        evidence = (
            field_metadata.get("evidence") if isinstance(field_metadata, dict) else None
        )
        block_id = evidence.get("block_id") if isinstance(evidence, dict) else None
        if not isinstance(block_id, str) or block_id.isdigit():
            continue
        terms = [term.strip() for term in block_id.split(";") if term.strip()]
        matching_pages = [
            page
            for page in company["pages"]
            if terms
            and all(term in str(page.get("location_label") or "") for term in terms)
        ]
        if len(matching_pages) == 1:
            evidence["block_id"] = matching_pages[0]["page_number"]
    data = MODEL.model_validate(parsed).model_dump(mode="json")
    expected_company = {
        "name": company["company_name"],
        "ticker": company["company_id"],
    }
    if data["company"] != expected_company:
        raise ValueError("Image extraction company identity changed.")
    if data["reporting_year"] != company["reporting_year"]:
        raise ValueError("Image extraction reporting year changed.")
    allowed_pages = {page["page_number"] for page in company["pages"]}
    rejected_fields = []
    for field, value in list(data["values"].items()):
        if value is None:
            continue
        try:
            _validate_field(
                field,
                value,
                data["metadata"][field],
                allowed_pages,
                data["reporting_year"],
            )
        except (KeyError, TypeError, ValueError) as error:
            rejected_fields.append({"field": field, "reason": str(error)})
            data["values"][field] = None
            data["metadata"][field] = {
                "status": "conflicting",
                "reporting_year": data["reporting_year"],
                "reason": str(error),
                "qualification": None,
                "confidence": None,
                "evidence": None,
            }
    data = MODEL.model_validate(data).model_dump(mode="json")
    ground_truth_checks = None
    disagreements = []
    if expected_values is not None:
        current_expected = {field: expected_values.get(field) for field in FIELDS}
        for field in FIELDS:
            actual = data["values"][field]
            expected = current_expected[field]
            if actual is None or expected is None:
                matches = actual is expected
            else:
                matches = math.isclose(
                    float(actual), float(expected), rel_tol=0.00001, abs_tol=0.000001
                )
            if not matches:
                disagreements.append(
                    {"field": field, "expected": expected, "actual": actual}
                )
        ground_truth_checks = not disagreements
    return data, {
        "status": "passed" if ground_truth_checks is not False else "failed",
        "schema_checks": True,
        "source_identity_checks": True,
        "image_source_checks": True,
        "ground_truth_checks": ground_truth_checks,
        "disagreements": disagreements,
        "rejected_fields": rejected_fields,
        "populated_fields": sum(value is not None for value in data["values"].values()),
    }


async def extract(
    company,
    client,
    output_root,
    *,
    provider,
    expected_values=None,
    quality_gate_version=None,
):
    """Call one multimodal model and save an immutable, reviewable extraction attempt."""
    provider = require_safe_path_segment(provider, "Provider")
    company_id = require_safe_path_segment(company["company_id"], "Company ID")
    client_identity = completion_client_identity(client)
    validate_company_pages(company)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "image_input_version": IMAGE_INPUT_VERSION,
                "source_sha256": company["source_sha256"],
                "image_sha256": [page["image_sha256"] for page in company["pages"]],
                "company_cik": company["company_cik"],
                "provider": provider,
                "client": client_identity,
                "fields": FIELDS,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    directory = (
        Path(output_root)
        / "environment_report"
        / "images"
        / provider
        / company_id
        / fingerprint
    )
    reusable = reusable_successful_receipt(
        directory,
        "data.json",
        required_filenames=("evidence.json", "validation.json", "source_manifest.json"),
    )
    if reusable is not None:
        return reusable
    unknown = retained_unknown_receipt(directory)
    if unknown is not None:
        return unknown
    attempt_id, attempt_directory = create_attempt_directory(directory)
    receipt_path = attempt_directory / "receipt.json"
    receipt = {
        "version": VERSION,
        "image_page_extraction": True,
        "image_count": len(company["pages"]),
        "category": "environment_report",
        "company_id": company_id,
        "company_cik": company["company_cik"],
        "document_id": company["document_id"],
        "source_sha256": company["source_sha256"],
        "source_url": company["source_url"],
        "model": client.model,
        "provider": provider,
        "base_url": client_identity["endpoint"],
        "model_options": client_identity["options"],
        "fingerprint": fingerprint,
        "attempt_id": attempt_id,
        "attempt_directory": str(attempt_directory),
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "calls": [],
    }
    save_json(receipt_path, receipt)
    provider_outcome_unknown = False
    try:
        save_json(attempt_directory / "source_manifest.json", request_summary(company))
        messages = extraction_messages(company)
        save_json(
            attempt_directory / "request.json",
            {
                "model": client.model,
                "base_url": client_identity["endpoint"],
                "model_options": client_identity["options"],
                "request": request_summary(company),
            },
        )
        started = time.monotonic()
        try:
            response = await client.complete(
                messages, operation="environment_page_image_extraction"
            )
        except (httpx.TransportError, TimeoutError):
            provider_outcome_unknown = True
            raise
        save_json(attempt_directory / "response.json", response)
        receipt["calls"].append(
            {
                "seconds": round(time.monotonic() - started, 3),
                "usage": response.get(
                    "_green500_gateway_usage", response.get("usage", {})
                ),
            }
        )
        data, validation = validate_result(
            completion_text(response), company, expected_values
        )
        if expected_values is None and quality_gate_version == QUALITY_GATE_VERSION:
            validation["quality_gate_version"] = QUALITY_GATE_VERSION
        save_json(attempt_directory / "validation.json", validation)
        if validation["status"] != "passed":
            raise ValueError(
                "Image extraction disagrees with independent sample truth."
            )
        save_json(attempt_directory / "data.json", data)
        save_json(
            attempt_directory / "evidence.json",
            {
                "source_sha256": company["source_sha256"],
                "source_url": company["source_url"],
                "pages": request_summary(company)["pages"],
                "citations": {
                    field: metadata["evidence"]
                    for field, metadata in data["metadata"].items()
                    if metadata and metadata.get("evidence")
                },
            },
        )
        receipt.update(
            status="succeeded",
            populated_fields=validation["populated_fields"],
            ground_truth_checks=validation["ground_truth_checks"],
            quality_gate_version=validation.get("quality_gate_version"),
            output_sha256=hashlib.sha256(
                (attempt_directory / "data.json").read_bytes()
            ).hexdigest(),
        )
    except Exception as error:  # noqa: BLE001 - every paid attempt needs a durable receipt
        receipt.update(
            status="unknown" if provider_outcome_unknown else "failed",
            error_type=type(error).__name__,
            error=(
                "Model request ended without a provider response; outcome is unknown."
                if provider_outcome_unknown
                else str(error)[:1500]
            ),
        )
    finally:
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        save_json(receipt_path, receipt)
        publish_attempt(
            directory,
            attempt_directory,
            receipt_only=receipt["status"] != "succeeded",
        )
    return receipt


async def run_batch(
    companies,
    clients,
    output_root,
    expected_by_company=None,
    concurrency_per_provider=2,
    quality_gate_version=None,
):
    """Run every provider over the same bounded QA sample with live progress receipts."""
    semaphores = {
        provider: asyncio.Semaphore(concurrency_per_provider) for provider in clients
    }
    results = []
    output_root = Path(output_root)

    async def one(company, provider, client):
        async with semaphores[provider]:
            result = await extract(
                company,
                client,
                output_root,
                provider=provider,
                expected_values=(expected_by_company or {}).get(company["company_id"]),
                quality_gate_version=quality_gate_version,
            )
            results.append(result)
            save_json(
                output_root / "environment_report" / "images" / "progress.json",
                {
                    "total": len(companies) * len(clients),
                    "completed": len(results),
                    "results": results,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
            print(
                json.dumps(
                    {
                        "company_id": company["company_id"],
                        "provider": provider,
                        "status": result["status"],
                        "completed": len(results),
                        "total": len(companies) * len(clients),
                    }
                ),
                flush=True,
            )

    await asyncio.gather(
        *(
            one(company, provider, client)
            for company in companies
            for provider, client in clients.items()
        )
    )
    return results
