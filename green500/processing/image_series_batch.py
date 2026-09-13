"""Merge several bounded image calls into one fixed environmental record."""

import asyncio
import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from green500.processing.chat_completion import completion_text
from green500.processing.environment_features import FIELDS, MODEL
from green500.processing.files import (
    completion_client_identity,
    create_attempt_directory,
    publish_attempt,
    require_safe_path_segment,
    retained_unknown_receipt,
    reusable_successful_receipt,
    save_json,
)
from green500.processing.image_report_batch import validate_result
from green500.processing.report_page_images import (
    VERSION as IMAGE_INPUT_VERSION,
)
from green500.processing.report_page_images import (
    extraction_messages,
    validate_company_pages,
)

VERSION = "image-series-environment-v2"
MAXIMUM_IMAGES_PER_CALL = 2


def _same_value(left, right):
    return math.isclose(float(left), float(right), rel_tol=0.00001, abs_tol=0.000001)


def _merge_results(company, call_results):
    """Union agreeing field evidence and surface every cross-call conflict."""
    values = dict.fromkeys(FIELDS)
    metadata = dict.fromkeys(FIELDS)
    conflicts = []
    citations = {}
    boundaries = []
    limitations = []
    call_results = sorted(
        call_results,
        key=lambda item: (item["call_index"], item["provider"], item["model"]),
    )
    for result in call_results:
        data = result["data"]
        if data.get("boundary") and data["boundary"] not in boundaries:
            boundaries.append(data["boundary"])
        for limitation in data.get("limitations", []):
            if limitation not in limitations:
                limitations.append(limitation)
    for field in FIELDS:
        candidates = []
        for result in call_results:
            value = result["data"]["values"][field]
            if value is not None:
                candidates.append(
                    {
                        "value": value,
                        "metadata": result["data"]["metadata"][field],
                        "provider": result["provider"],
                        "call_index": result["call_index"],
                        "pages": result["pages"],
                    }
                )
        distinct = []
        for candidate in candidates:
            if not any(
                _same_value(candidate["value"], item["value"]) for item in distinct
            ):
                distinct.append(candidate)
        if len(distinct) > 1:
            conflicts.append(
                {
                    "field": field,
                    "candidates": [
                        {
                            "value": item["value"],
                            "provider": item["provider"],
                            "call_index": item["call_index"],
                        }
                        for item in candidates
                    ],
                }
            )
            metadata[field] = {
                "status": "conflicting",
                "reporting_year": company["reporting_year"],
                "reason": "Image calls returned different values for this field.",
                "qualification": None,
                "confidence": None,
                "evidence": None,
            }
            continue
        if not candidates:
            metadata[field] = {
                "status": "not_extracted",
                "reporting_year": company["reporting_year"],
                "reason": "No supplied image call established this field.",
                "qualification": None,
                "confidence": None,
                "evidence": None,
            }
            continue
        candidates.sort(
            key=lambda item: (
                item["metadata"]["evidence"]["block_id"],
                item["provider"],
                item["call_index"],
            )
        )
        accepted = candidates[0]
        cited_page = next(
            page
            for page in accepted["pages"]
            if page["page_number"] == accepted["metadata"]["evidence"]["block_id"]
        )
        values[field] = accepted["value"]
        metadata[field] = accepted["metadata"]
        citations[field] = {
            **accepted["metadata"]["evidence"],
            "provider": accepted["provider"],
            "call_index": accepted["call_index"],
            "page_number": cited_page["page_number"],
            "image_sha256": cited_page["image_sha256"],
            "image_byte_count": cited_page["image_byte_count"],
            "agreeing_candidate_count": len(candidates),
        }
    data = MODEL.model_validate(
        {
            "company": {
                "name": company["company_name"],
                "ticker": company["company_id"],
            },
            "reporting_year": company["reporting_year"],
            "boundary": boundaries[0] if len(boundaries) == 1 else None,
            "limitations": sorted(
                set(
                    limitations
                    + (
                        ["Image calls reported different company boundaries."]
                        if len(boundaries) > 1
                        else []
                    )
                )
            ),
            "values": values,
            "metadata": metadata,
        }
    ).model_dump(mode="json")
    return data, citations, conflicts


def _compare_ground_truth(data, expected_values):
    disagreements = []
    if expected_values is None:
        return None, disagreements
    for field in FIELDS:
        expected = expected_values.get(field)
        actual = data["values"][field]
        matches = (
            actual is expected
            if actual is None or expected is None
            else _same_value(actual, expected)
        )
        if not matches:
            disagreements.append(
                {"field": field, "expected": expected, "actual": actual}
            )
    return not disagreements, disagreements


async def extract(company, call_groups, clients, output_root, *, expected_values=None):
    """Run all page groups and providers concurrently, updating one live aggregate receipt."""
    company_id = require_safe_path_segment(company["company_id"], "Company ID")
    if not clients:
        raise ValueError("Image-series extraction requires at least one provider.")
    client_identities = {}
    for provider, client in clients.items():
        safe_provider = require_safe_path_segment(provider, "Provider")
        client_identities[safe_provider] = completion_client_identity(client)
    if not call_groups or any(
        not 1 <= len(group) <= MAXIMUM_IMAGES_PER_CALL for group in call_groups
    ):
        raise ValueError("Each image-series call requires one or two page images.")
    all_pages = {}
    for group in call_groups:
        for page in group:
            previous = all_pages.get(page["page_number"])
            if previous and previous["image_sha256"] != page["image_sha256"]:
                raise ValueError("One page number maps to different image hashes.")
            all_pages[page["page_number"]] = page
    canonical_groups = [
        sorted(group, key=lambda page: (page["page_number"], page["image_sha256"]))
        for group in call_groups
    ]
    canonical_groups.sort(
        key=lambda group: [
            (page["page_number"], page["image_sha256"]) for page in group
        ]
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "image_input_version": IMAGE_INPUT_VERSION,
                "source_sha256": company["source_sha256"],
                "company_cik": company["company_cik"],
                "company_id": company_id,
                "document_id": company["document_id"],
                "reporting_year": company["reporting_year"],
                "call_groups": [
                    [page["image_sha256"] for page in group]
                    for group in canonical_groups
                ],
                "clients": client_identities,
                "fields": FIELDS,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    directory = (
        Path(output_root)
        / "environment_report"
        / "image-series"
        / company_id
        / fingerprint
    )
    reusable = reusable_successful_receipt(
        directory,
        "data.json",
        required_filenames=("evidence.json", "validation.json"),
    )
    if reusable is not None:
        return reusable
    unknown = retained_unknown_receipt(directory)
    if unknown is not None:
        return unknown
    attempt_id, attempt_directory = create_attempt_directory(directory)
    receipt_path = attempt_directory / "receipt.json"
    total_calls = len(canonical_groups) * len(clients)
    receipt = {
        "version": VERSION,
        "image_series_extraction": True,
        "maximum_images_per_call": MAXIMUM_IMAGES_PER_CALL,
        "image_count": len(all_pages),
        "category": "environment_report",
        "company_id": company_id,
        "company_cik": company["company_cik"],
        "document_id": company["document_id"],
        "source_sha256": company["source_sha256"],
        "source_url": company["source_url"],
        "model": " + ".join(clients[provider].model for provider in sorted(clients)),
        "provider": " + ".join(sorted(clients)),
        "clients": client_identities,
        "fingerprint": fingerprint,
        "attempt_id": attempt_id,
        "attempt_directory": str(attempt_directory),
        "status": "running",
        "calls_completed": 0,
        "calls_total": total_calls,
        "started_at": datetime.now(UTC).isoformat(),
        "calls": [],
    }
    save_json(receipt_path, receipt)
    lock = asyncio.Lock()
    results = []

    async def one(call_index, pages, provider, client):
        item = {**company, "pages": pages}
        validate_company_pages(item)
        call_directory = attempt_directory / "calls" / str(call_index) / provider
        call_directory.mkdir(parents=True, exist_ok=True)
        save_json(call_directory / "pages.json", {"pages": pages})
        save_json(
            call_directory / "receipt.json",
            {
                "status": "running",
                "provider": provider,
                "model": client.model,
                "base_url": client_identities[provider]["endpoint"],
                "model_options": client_identities[provider]["options"],
                "call_index": call_index,
                "started_at": datetime.now(UTC).isoformat(),
            },
        )
        started = time.monotonic()
        try:
            response = await client.complete(
                extraction_messages(item), operation="environment_image_series"
            )
            save_json(call_directory / "response.json", response)
            data, validation = validate_result(completion_text(response), item)
            save_json(call_directory / "data.json", data)
            save_json(call_directory / "validation.json", validation)
            result = {
                "status": "succeeded",
                "provider": provider,
                "model": client.model,
                "call_index": call_index,
                "pages": [
                    {
                        key: page[key]
                        for key in ("page_number", "image_sha256", "image_byte_count")
                    }
                    for page in pages
                ],
                "seconds": round(time.monotonic() - started, 3),
                "data": data,
            }
            results.append(result)
        except Exception as error:  # noqa: BLE001 - keep partial page coverage auditable
            provider_outcome_unknown = isinstance(
                error, (httpx.TransportError, TimeoutError)
            )
            result = {
                "status": "unknown" if provider_outcome_unknown else "failed",
                "provider": provider,
                "model": client.model,
                "call_index": call_index,
                "pages": [
                    {
                        key: page[key]
                        for key in ("page_number", "image_sha256", "image_byte_count")
                    }
                    for page in pages
                ],
                "seconds": round(time.monotonic() - started, 3),
                "error": (
                    "Model request ended without a provider response; outcome is unknown."
                    if provider_outcome_unknown
                    else str(error)[:1000]
                ),
            }
        save_json(call_directory / "receipt.json", result)
        async with lock:
            receipt["calls_completed"] += 1
            receipt["calls"].append(
                {key: value for key, value in result.items() if key != "data"}
            )
            save_json(receipt_path, receipt)

    await asyncio.gather(
        *(
            one(index, pages, provider, client)
            for index, pages in enumerate(canonical_groups, 1)
            for provider, client in sorted(clients.items())
        )
    )
    successful = [result for result in results if result["status"] == "succeeded"]
    covered_groups = set()
    try:
        if not successful:
            raise ValueError("Every image-series model call failed.")
        covered_groups = {result["call_index"] for result in successful}
        if covered_groups != set(range(1, len(canonical_groups) + 1)):
            raise ValueError(
                "At least one planned image group has no successful provider result."
            )
        data, citations, conflicts = _merge_results(company, successful)
        ground_truth_checks, disagreements = _compare_ground_truth(
            data, expected_values
        )
        validation = {
            "status": "passed" if ground_truth_checks is not False else "failed",
            "schema_checks": True,
            "source_identity_checks": True,
            "image_source_checks": True,
            "image_series_checks": True,
            "ground_truth_checks": ground_truth_checks,
            "conflicts": conflicts,
            "disagreements": disagreements,
            "populated_fields": sum(
                value is not None for value in data["values"].values()
            ),
        }
        save_json(attempt_directory / "validation.json", validation)
        if validation["status"] != "passed":
            raise ValueError(
                "Merged image series disagrees with independent source truth."
            )
        save_json(attempt_directory / "data.json", data)
        save_json(
            attempt_directory / "evidence.json",
            {
                "source_sha256": company["source_sha256"],
                "source_url": company["source_url"],
                "pages": [
                    {
                        key: page[key]
                        for key in ("page_number", "image_sha256", "image_byte_count")
                    }
                    for page in all_pages.values()
                ],
                "citations": citations,
            },
        )
        receipt.update(
            status="succeeded",
            populated_fields=validation["populated_fields"],
            ground_truth_checks=ground_truth_checks,
            output_sha256=hashlib.sha256(
                (attempt_directory / "data.json").read_bytes()
            ).hexdigest(),
        )
    except Exception as error:  # noqa: BLE001 - aggregate failure remains visible
        unknown_uncovered = any(
            result["status"] == "unknown" and result["call_index"] not in covered_groups
            for result in receipt["calls"]
        )
        receipt.update(
            status="unknown" if unknown_uncovered else "failed",
            error_type=type(error).__name__,
            error=(
                "At least one planned image group has an unknown provider outcome."
                if unknown_uncovered
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
