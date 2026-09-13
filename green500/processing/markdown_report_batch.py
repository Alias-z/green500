"""Run fixed extraction from bounded Markdown tables and source excerpts."""

import asyncio
import fcntl
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from green500.processing.chat_completion import completion_text
from green500.processing.files import (
    completion_client_identity,
    create_attempt_directory,
    publish_attempt,
    require_safe_path_segment,
    retained_unknown_receipt,
    reusable_successful_receipt,
    save_json,
)
from green500.processing.fixed_report_batch import (
    category_profile,
    extraction_messages,
    validate_result,
)
from green500.processing.report_table_markdown import VERSION as MARKDOWN_VERSION
from green500.processing.report_table_markdown import prepare_report_markdown

VERSION = "targeted-report-markdown-v2"


def _remove_unrepresentable_comparison_values(data, evidence):
    """Remove source bounds when a fixed environment field supports only a point."""
    adjustments = []
    for field, value in data["values"].items():
        if value is None:
            continue
        proof = evidence["citations"].get(field) or {}
        raw = str(proof.get("raw_value") or "").replace(",", "")
        matching_lines = [
            line
            for line in str(proof.get("quote") or "").splitlines()
            if raw and raw in line.replace(",", "")
        ]
        if len(matching_lines) != 1 or not re.search(
            r"(?:[<>≤≥]\s*"
            + re.escape(raw)
            + r"\b|\b(?:at least|at most|more than|less than)\b)",
            matching_lines[0],
            re.IGNORECASE,
        ):
            continue
        reason = "A comparison bound cannot populate a fixed point-value field."
        metadata = data["metadata"][field] or {}
        data["values"][field] = None
        data["metadata"][field] = {
            "status": "not_extracted",
            "reporting_year": metadata.get("reporting_year"),
            "reason": reason,
            "qualification": metadata.get("qualification"),
            "confidence": None,
            "evidence": None,
        }
        evidence["citations"].pop(field, None)
        evidence["omitted_fields"].append({"field": field, "reason": reason})
        adjustments.append({"field": field, "reason": reason})
    return adjustments


def prepare(item, output_directory, *, extraction_group=None):
    """Create and save one bounded Markdown input from the original report."""
    company_id = require_safe_path_segment(item["company_id"], "Company ID")
    source_sha256 = item.get("sha256")
    if (
        not isinstance(source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
    ):
        raise ValueError("Source SHA-256 must contain exactly 64 lowercase hex digits.")
    if extraction_group is not None:
        extraction_group = require_safe_path_segment(
            extraction_group, "Extraction group"
        )
    profile = category_profile(item["category"])
    if extraction_group:
        from green500.processing.environment_group_profile import (
            environment_group_profile,
        )

        profile = environment_group_profile(extraction_group)
    prepared = prepare_report_markdown(
        item,
        profile.KEYWORDS,
        # Leave room for separators between many short HTML-row sections.
        maximum_characters=17_500 if extraction_group else 45_000,
        maximum_pages=4 if extraction_group else 10,
    )
    if prepared.get("version") != MARKDOWN_VERSION:
        raise ValueError("Prepared input uses an unexpected Markdown version.")
    if prepared.get("source_sha256") != item["sha256"]:
        raise ValueError("Prepared input does not match the source manifest.")
    if not prepared.get("blocks") or not prepared.get("markdown"):
        raise ValueError("Prepared input contains no source evidence.")
    character_limit = 18_000 if extraction_group else 45_000
    while len(prepared["markdown"]) > character_limit and prepared["blocks"]:
        prepared["blocks"].pop()
        prepared["markdown"] = "\n\n".join(
            block["text"] for block in prepared["blocks"]
        )
    prepared["selected_pages"] = sorted(
        {
            block["locator"]["page"]
            for block in prepared["blocks"]
            if block["locator"].get("page") is not None
        }
    )
    if len(prepared["markdown"]) > character_limit:
        raise ValueError(
            f"Prepared Markdown exceeds the {character_limit:,}-character limit."
        )
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    suffix = "-" + extraction_group if extraction_group else ""
    path = output_directory / f"{company_id}-{source_sha256}{suffix}.json"
    save_json(path, prepared)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "version": prepared["version"],
        "character_count": len(prepared["markdown"]),
        "block_count": len(prepared["blocks"]),
        "selected_pages": prepared.get("selected_pages", []),
        "warnings": prepared.get("warnings", []),
    }


def load_prepared(item, prepared_path):
    """Load a saved Markdown input without requiring PDF dependencies at model runtime."""
    path = Path(prepared_path)
    raw = path.read_bytes()
    prepared = json.loads(raw)
    if prepared.get("version") != MARKDOWN_VERSION:
        raise ValueError("Saved Markdown input version changed.")
    if prepared.get("source_sha256") != item["sha256"]:
        raise ValueError("Saved Markdown input differs from the source manifest.")
    if not prepared.get("blocks") or not prepared.get("markdown"):
        raise ValueError("Saved Markdown input contains no evidence.")
    if len(prepared["markdown"]) > 45_000:
        raise ValueError("Saved Markdown input exceeds the character limit.")
    evidence = {
        "sha256": item["sha256"],
        "source_url": item["source_url"],
        "document_name": item["source_document"],
        "text_format_version": MARKDOWN_VERSION,
        "blocks": prepared["blocks"],
        "source_text_complete": False,
        "all_text_selected": False,
        "selected_characters": len(prepared["markdown"]),
        "warnings": prepared.get("warnings", []),
    }
    return prepared, evidence, hashlib.sha256(raw).hexdigest()


async def extract(
    item,
    prepared_path,
    client,
    output_root,
    *,
    provider,
    retry_failed=False,
    extraction_group=None,
):
    """Extract and source-validate one prepared report with one correction round."""
    company_id = require_safe_path_segment(item["company_id"], "Company ID")
    category = require_safe_path_segment(item["category"], "Category")
    provider = require_safe_path_segment(provider, "Provider")
    if extraction_group is not None:
        extraction_group = require_safe_path_segment(
            extraction_group, "Extraction group"
        )
    client_identity = completion_client_identity(client)
    profile = category_profile(category)
    if extraction_group:
        from green500.processing.environment_group_profile import (
            environment_group_profile,
        )

        profile = environment_group_profile(extraction_group)
    prepared, evidence, prepared_sha256 = load_prepared(item, prepared_path)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "markdown_version": MARKDOWN_VERSION,
                "prepared_sha256": prepared_sha256,
                "source_sha256": item["sha256"],
                "category": category,
                "company_cik": item["company_cik"],
                "provider": provider,
                "extraction_group": extraction_group,
                "model": client.model,
                "endpoint": client_identity["endpoint"],
                "options": client_identity["options"],
                "instructions": profile.INSTRUCTIONS,
                "schema": profile.MODEL.model_json_schema(),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    directory = (
        Path(output_root) / category / "markdown" / provider / company_id / fingerprint
    )
    receipt_path = directory / "receipt.json"
    if receipt_path.exists():
        prior = json.loads(receipt_path.read_text())
        if prior.get("status") == "succeeded":
            reusable = reusable_successful_receipt(
                directory,
                "data.json",
                required_filenames=(
                    "evidence.json",
                    "validation.json",
                    "source_manifest.json",
                    "prepared_input.json",
                    "source_blocks.json",
                ),
            )
            if reusable is not None:
                return reusable
        if prior.get("status") == "unknown" or not retry_failed:
            return {**prior, "reused": True}
    unknown = retained_unknown_receipt(directory)
    if unknown is not None:
        return unknown
    attempt_id, attempt_directory = create_attempt_directory(directory)
    receipt_path = attempt_directory / "receipt.json"
    receipt = {
        "version": VERSION,
        "markdown_input_version": MARKDOWN_VERSION,
        "table_markdown_extraction": True,
        "category": category,
        "company_id": company_id,
        "company_cik": item["company_cik"],
        "document_id": item["document_id"],
        "source_sha256": item["sha256"],
        "source_url": item["source_url"],
        "prepared_sha256": prepared_sha256,
        "model": client.model,
        "provider": provider,
        "base_url": client_identity["endpoint"],
        "model_options": client_identity["options"],
        "extraction_group": extraction_group,
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
        save_json(attempt_directory / "source_blocks.json", evidence)
        save_json(attempt_directory / "prepared_input.json", prepared)
        messages = extraction_messages(item, evidence, profile)
        for attempt in range(2):
            operation = "extraction" if attempt == 0 else "correction"
            save_json(
                attempt_directory / f"{operation}.request.json",
                {
                    "model": client.model,
                    "base_url": client_identity["endpoint"],
                    "model_options": client_identity["options"],
                    "messages": messages,
                },
            )
            started = time.monotonic()
            try:
                response = await client.complete(
                    messages,
                    operation=f"markdown_{item['category']}_{operation}",
                )
            except (httpx.TransportError, TimeoutError):
                provider_outcome_unknown = True
                raise
            save_json(attempt_directory / f"{operation}.response.json", response)
            receipt["calls"].append(
                {
                    "operation": operation,
                    "seconds": round(time.monotonic() - started, 3),
                    "usage": response.get("usage", {}),
                }
            )
            save_json(receipt_path, receipt)
            try:
                data, citations = validate_result(
                    completion_text(response), item, evidence, profile
                )
                comparison_adjustments = []
                if item["category"] == "environment_report":
                    comparison_adjustments = _remove_unrepresentable_comparison_values(
                        data, citations
                    )
                profile.MODEL.model_validate(data)
                break
            except (KeyError, TypeError, ValueError) as error:
                if attempt:
                    raise
                messages = extraction_messages(item, evidence, profile) + [
                    {
                        "role": "assistant",
                        "content": response.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content")
                        or "",
                    },
                    {
                        "role": "user",
                        "content": (
                            "Correct this schema or source-evidence error using only the original supplied evidence: "
                            + str(error)[:2_500]
                        ),
                    },
                ]
        save_json(attempt_directory / "data.json", data)
        citations["comparison_bound_adjustments"] = comparison_adjustments
        save_json(attempt_directory / "evidence.json", citations)
        save_json(
            attempt_directory / "validation.json",
            {
                "status": "passed",
                "schema_checks": True,
                "source_identity_checks": True,
                "source_quote_checks": True,
                "table_markdown_source_checks": True,
                "reviewed_by_model": False,
                "populated_fields": len(citations["citations"]),
            },
        )
        save_json(
            attempt_directory / "source_manifest.json",
            {
                key: item[key]
                for key in (
                    "category",
                    "company_id",
                    "company_cik",
                    "company_name",
                    "document_id",
                    "sha256",
                    "source_document",
                    "source_url",
                )
            },
        )
        receipt.update(
            status="succeeded",
            populated_fields=len(citations["citations"]),
            output_sha256=hashlib.sha256(
                (attempt_directory / "data.json").read_bytes()
            ).hexdigest(),
        )
    except Exception as error:  # noqa: BLE001 - each sample needs a durable receipt
        receipt.update(
            status="unknown" if provider_outcome_unknown else "failed",
            error_type=type(error).__name__,
            error=(
                "Model request ended without a provider response; outcome is unknown."
                if provider_outcome_unknown
                else str(error)[:1_500]
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
    items,
    prepared_manifest,
    clients,
    output_root,
    *,
    concurrency_per_provider=3,
    extraction_group=None,
):
    """Run disjoint provider assignments from already prepared inputs."""
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    prepared_by_company = {row["company_id"]: row for row in prepared_manifest}
    semaphores = {
        provider: asyncio.Semaphore(concurrency_per_provider) for provider in clients
    }
    results = []
    lock_path = output_root / ".markdown-batch.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        async def one(item):
            provider = item["assigned_provider"]
            async with semaphores[provider]:
                result = await extract(
                    item,
                    prepared_by_company[item["company_id"]]["path"],
                    clients[provider],
                    output_root,
                    provider=provider,
                    retry_failed=True,
                    extraction_group=extraction_group,
                )
                results.append(result)
                save_json(
                    output_root / "progress.json",
                    {
                        "total": len(items),
                        "completed": len(results),
                        "results": results,
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                )
                print(
                    json.dumps(
                        {
                            "company_id": item["company_id"],
                            "provider": provider,
                            "status": result["status"],
                            "completed": len(results),
                            "total": len(items),
                        }
                    ),
                    flush=True,
                )

        await asyncio.gather(*(one(item) for item in items))
    return results
