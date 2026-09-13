"""Validate and publish ten fixed social fields from at most two page images."""

import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from green500.processing.chat_completion import completion_text
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
from green500.processing.image_report_batch import (
    _normalize_transport_variants,
    _parse_json,
)
from green500.processing.report_page_images import (
    request_summary,
    validate_company_pages,
)
from green500.processing.social_features import FIELDS, MODEL
from green500.processing.social_page_images import (
    VERSION as SOCIAL_IMAGE_INPUT_VERSION,
)
from green500.processing.social_page_images import (
    extraction_messages,
)
from green500.processing.source_quotes import normalize_source_text

VERSION = "image-page-social-v2"


def _validate_field(field, value, metadata, allowed_pages, reporting_year):
    """Check one social value's unit, population, year and visible page citation."""
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
    normalized_quote = normalize_source_text(quote).replace(",", "")
    if (
        not raw_value
        or normalize_source_text(raw_value).replace(",", "") not in normalized_quote
    ):
        raise ValueError(field + ": raw value is absent from the visible-row quote.")
    raw_number = _parse_fixed_raw_number(raw_value)
    target_unit = FIELDS[field]["unit"]
    if target_unit.startswith("rate per "):
        compact_quote = re.sub(r"[,\s]", "", normalize_source_text(quote))
        denominator = target_unit.split()[2]
        if (
            str(evidence.get("source_unit")) != target_unit
            or denominator not in compact_quote
        ):
            raise ValueError(
                field + ": injury-rate denominator is not explicit in the quote."
            )
        converted = raw_number
    else:
        converted = convert(raw_number, evidence.get("source_unit"), target_unit)
    if converted is None or not math.isclose(
        float(converted), float(value), rel_tol=0.00001, abs_tol=0.000001
    ):
        raise ValueError(field + ": source unit does not produce the canonical value.")
    scale_factor = evidence.get("scale_factor")
    if not isinstance(scale_factor, (int, float)) or not math.isclose(
        raw_number * scale_factor, float(value), rel_tol=0.00001, abs_tol=0.000001
    ):
        raise ValueError(
            field + ": conversion scale disagrees with the canonical value."
        )
    context = normalize_source_text(
        " ".join([quote, str(metadata.get("qualification") or "")])
    )
    requirements = {
        "social_employee_turnover_percent": ("turnover",),
        "social_women_workforce_percent": ("women", "workforce"),
        "social_employee_fatalities_count": ("employee", "fatalit"),
        "social_contractor_fatalities_count": ("contractor", "fatalit"),
        "social_recordable_injury_rate_per_200000_hours": (
            "employee",
            "recordable",
            "200000",
        ),
        "social_recordable_injury_rate_per_1000000_hours": (
            "employee",
            "recordable",
            "1000000",
        ),
        "social_suppliers_audited_count": ("supplier", "audit"),
        "social_confirmed_violations_count": ("confirm", "violation"),
        "social_community_investment_usd": ("community",),
    }
    compact_context = re.sub(r"[,\s]", "", context)
    if field == "social_employees_count" and not any(
        term in compact_context for term in ("employee", "workforce")
    ):
        raise ValueError(
            field + ": quote does not state an employee or workforce total."
        )
    if any(term not in compact_context for term in requirements.get(field, ())):
        raise ValueError(
            field
            + ": quote does not establish the requested social metric and boundary."
        )
    if field == "social_women_workforce_percent" and any(
        term in context for term in ("management", "manager", "board")
    ):
        raise ValueError(
            field + ": leadership representation is not total workforce representation."
        )


def validate_result(text, company, expected_values=None):
    """Validate Qwen social output against the schema, images and optional sample truth."""
    parsed = _normalize_transport_variants(_parse_json(text))
    data = MODEL.model_validate(parsed).model_dump(mode="json")
    if data["company"] != {
        "name": company["company_name"],
        "ticker": company["company_id"],
    }:
        raise ValueError("Social image extraction company identity changed.")
    if data["reporting_year"] != company["reporting_year"]:
        raise ValueError("Social image extraction reporting year changed.")
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
    disagreements = []
    ground_truth_checks = None
    if expected_values is not None:
        for field in FIELDS:
            expected = expected_values.get(field)
            actual = data["values"][field]
            matches = (
                actual is expected
                if actual is None or expected is None
                else math.isclose(
                    float(actual), float(expected), rel_tol=0.00001, abs_tol=0.000001
                )
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
        "populated_fields": sum(value is not None for value in data["values"].values()),
        "rejected_fields": rejected_fields,
        "disagreements": disagreements,
    }


async def extract(company, client, output_root, *, provider, expected_values=None):
    """Save a live receipt, one paid response and its deterministic validation."""
    provider = require_safe_path_segment(provider, "Provider")
    company_id = require_safe_path_segment(company["company_id"], "Company ID")
    client_identity = completion_client_identity(client)
    validate_company_pages(company)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "image_input_version": SOCIAL_IMAGE_INPUT_VERSION,
                "source_sha256": company["source_sha256"],
                "images": [page["image_sha256"] for page in company["pages"]],
                "provider": provider,
                "client": client_identity,
                "fields": FIELDS,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    directory = (
        Path(output_root)
        / "social_employee"
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
        "image_input_version": SOCIAL_IMAGE_INPUT_VERSION,
        "image_page_extraction": True,
        "image_count": len(company["pages"]),
        "category": "social_employee",
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
        summary = request_summary(company)
        save_json(attempt_directory / "source_manifest.json", summary)
        save_json(
            attempt_directory / "request.json",
            {
                "model": client.model,
                "base_url": client_identity["endpoint"],
                "model_options": client_identity["options"],
                "request": summary,
            },
        )
        started = time.monotonic()
        try:
            response = await client.complete(
                extraction_messages(company), operation="social_page_image_extraction"
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
        save_json(attempt_directory / "validation.json", validation)
        if validation["status"] != "passed":
            raise ValueError(
                "Social image extraction disagrees with independent sample truth."
            )
        save_json(attempt_directory / "data.json", data)
        save_json(
            attempt_directory / "evidence.json",
            {
                "source_sha256": company["source_sha256"],
                "source_url": company["source_url"],
                "pages": summary["pages"],
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
            output_sha256=hashlib.sha256(
                (attempt_directory / "data.json").read_bytes()
            ).hexdigest(),
        )
    except Exception as error:  # noqa: BLE001 - every paid call needs a visible receipt
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
