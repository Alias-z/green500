"""Resumable concurrent financial extraction through a caller-configured chat API."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from dotenv import load_dotenv
from lxml import html

from green500.processing.chat_completion import ChatCompletionClient, completion_text
from green500.processing.financial_extraction import (
    SELECTION_VERSION,
    _source_fiscal_year_evidence,
    extraction_messages,
    prepare_financial_source,
    review_messages,
    source_fiscal_year,
    validate_financial_response,
    validate_review,
)
from green500.processing.financial_input import VERSION as MODEL_INPUT_VERSION
from green500.processing.financial_prompt import (
    PROMPT_VERSION,
    REVIEW_PROMPT,
    SYSTEM_PROMPT,
)
from green500.processing.financial_schema import VERDEXFinancialData
from green500.processing.input import VERSION as SOURCE_BLOCK_VERSION
from green500.processing.input import prepare_input

_SEC_ARCHIVE_DOCUMENT_PATH = re.compile(
    r"^/Archives/edgar/data/(?P<cik>[1-9]\d*)/"
    r"(?P<accession>\d{18})/(?P<filename>[^/]+)$"
)


def _sec_archive_document_identity(url):
    """Return the CIK, accession and filename from one exact SEC archive URL."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.sec.gov"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("SEC annual source URL is not an exact public archive URL.")
    match = _SEC_ARCHIVE_DOCUMENT_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError("SEC annual source URL has an invalid archive document path.")
    return match.groupdict()


def _verified_annual_report_wrapper(item):
    """Verify an annual wrapper hash, exact attachment link, accession and cover year."""
    wrapper = item.get("annual_report_wrapper")
    if wrapper is None:
        return None
    if not isinstance(wrapper, dict):
        raise TypeError("Annual report wrapper evidence must be an object.")
    required = {
        "fiscal_year",
        "wrapper_document_id",
        "wrapper_sha256",
        "wrapper_url",
        "wrapper_path",
        "attachment_document_id",
        "attachment_sha256",
        "attachment_url",
    }
    missing = sorted(required - set(wrapper))
    if missing:
        raise ValueError(f"Annual report wrapper is missing fields: {missing}")
    wrapper_sha256 = wrapper["wrapper_sha256"]
    if (
        not isinstance(wrapper_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", wrapper_sha256) is None
    ):
        raise ValueError("Annual report wrapper SHA-256 is invalid.")
    if (
        not isinstance(wrapper["wrapper_document_id"], int)
        or wrapper["wrapper_document_id"] <= 0
    ):
        raise ValueError("Annual report wrapper document ID is invalid.")
    if wrapper["attachment_document_id"] != item.get("document_id"):
        raise ValueError("Annual report wrapper names a different attachment document.")
    if wrapper["attachment_sha256"] != item.get("sha256"):
        raise ValueError("Annual report wrapper names a different attachment SHA-256.")
    if wrapper["attachment_url"] != item.get("source_url"):
        raise ValueError("Annual report wrapper names a different attachment URL.")

    wrapper_path = Path(wrapper["wrapper_path"])
    wrapper_body = wrapper_path.read_bytes()
    if hashlib.sha256(wrapper_body).hexdigest() != wrapper_sha256:
        raise ValueError("Annual report wrapper SHA-256 does not match its manifest.")
    wrapper_identity = _sec_archive_document_identity(wrapper["wrapper_url"])
    attachment_identity = _sec_archive_document_identity(item["source_url"])
    if (
        wrapper_identity["cik"] != attachment_identity["cik"]
        or wrapper_identity["accession"] != attachment_identity["accession"]
    ):
        raise ValueError(
            "Annual wrapper and attachment are not in the same SEC accession."
        )
    if item.get("company_cik") and int(item["company_cik"]) != int(
        wrapper_identity["cik"]
    ):
        raise ValueError("Annual wrapper SEC CIK disagrees with the company manifest.")

    document = html.fromstring(wrapper_body)
    matching_links = [
        link
        for link in document.xpath("//a[@href]")
        if urljoin(wrapper["wrapper_url"], link.get("href")) == item["source_url"]
    ]
    if len(matching_links) != 1:
        raise ValueError(
            "Annual wrapper must contain one exact link to the attachment."
        )
    link = matching_links[0]
    wrapper_full = prepare_input(
        wrapper_body,
        "text/html",
        wrapper_identity["filename"],
        wrapper["wrapper_url"],
    )
    cover = _source_fiscal_year_evidence(wrapper_full)
    if wrapper["fiscal_year"] != cover["fiscal_year"]:
        raise ValueError(
            "Annual wrapper fiscal year disagrees with its exact cover evidence."
        )
    return {
        "basis": "same_accession_sec_annual_wrapper",
        "fiscal_year": cover["fiscal_year"],
        "wrapper_document_id": wrapper["wrapper_document_id"],
        "wrapper_sha256": wrapper_sha256,
        "wrapper_url": wrapper["wrapper_url"],
        "wrapper_cover": cover,
        "attachment_document_id": item["document_id"],
        "attachment_sha256": item["sha256"],
        "attachment_url": item["source_url"],
        "sec_accession": wrapper_identity["accession"],
        "exact_wrapper_link": {
            "href": link.get("href"),
            "resolved_url": item["source_url"],
            "link_text": " ".join(" ".join(link.itertext()).split()),
        },
    }


def _full_source_with_wrapper_year(full, wrapper_evidence):
    """Add exact wrapper cover evidence without changing attachment block identifiers."""
    if wrapper_evidence is None:
        return full
    block_id = "W00000"
    if any(block.get("id") == block_id for block in full["blocks"]):
        raise ValueError(
            "Attachment source already uses the reserved wrapper block ID."
        )
    cover = wrapper_evidence["wrapper_cover"]
    combined = {**full, "blocks": list(full["blocks"])}
    combined["blocks"].append(
        {
            "id": block_id,
            "kind": "metadata",
            "location": "SEC annual wrapper",
            "text": cover["quote"],
            "context": "",
            "source_document_id": wrapper_evidence["wrapper_document_id"],
            "source_sha256": wrapper_evidence["wrapper_sha256"],
            "source_url": wrapper_evidence["wrapper_url"],
        }
    )
    combined["annual_report_cover"] = {
        "block_id": block_id,
        "quote": cover["quote"],
        "fiscal_year": cover["fiscal_year"],
    }
    return combined


def save_json(path, value):
    """Publish one complete JSON checkpoint by atomic replacement."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False, default=str)
        + "\n"
    )
    temporary.replace(path)


def annual_report_manifest(settings):
    """Select the latest saved SEC annual filing per current company, by filing date."""
    from green500 import db
    from green500.catalog import SOURCE_QUERY

    with db.connect(settings) as connection:
        companies = connection.execute(
            "SELECT cik,name,symbols FROM companies WHERE is_current ORDER BY name,cik"
        ).fetchall()
        sources = connection.execute(SOURCE_QUERY, (None, None)).fetchall()
    choices = {}
    for source in sources:
        if (
            not source.get("document_id")
            or "financial_report" not in source["categories"]
        ):
            continue
        match = re.search(
            r"\bSEC (10-K|20-F|40-F) filed (\d{4}-\d{2}-\d{2})\b", source["title"]
        )
        if not match:
            continue
        key = (match[2], source["document_id"])
        current = choices.get(source["company_cik"])
        if current is None or key > current[0]:
            choices[source["company_cik"]] = (key, source, match[1])
    manifest, missing = [], []
    for company in companies:
        current = choices.get(company["cik"])
        if current is None:
            missing.append(
                {
                    "company_cik": company["cik"],
                    "company_name": company["name"],
                    "reason": "No saved annual filing",
                }
            )
            continue
        _, source, form = current
        manifest.append(
            {
                "company_id": company["symbols"][0]
                if company["symbols"]
                else company["cik"],
                "company_cik": company["cik"],
                "company_name": company["name"],
                "document_id": source["document_id"],
                "sha256": source["sha256"],
                "source_document": source["title"],
                "source_url": source["url"],
                "content_type": source["content_type"],
                "filing_form": form,
                "filename": source["url"].rsplit("/", 1)[-1],
                "path": str(
                    settings.data_dir
                    / "objects"
                    / source["sha256"][:2]
                    / source["sha256"]
                ),
            }
        )
    return manifest, missing


async def extract_document(
    item, client, output_dir, *, resume=True, review_with_model=False
):
    """Preserve every request/response and publish source-checked financial data."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", item["company_id"]):
        raise ValueError(
            "Company ID must be a ticker or identifier without path separators."
        )
    body = Path(item["path"]).read_bytes()
    source_hash = hashlib.sha256(body).hexdigest()
    if item.get("sha256") and item["sha256"] != source_hash:
        raise ValueError("Original report SHA-256 does not match its manifest.")
    annual_report_wrapper = _verified_annual_report_wrapper(item)
    schema_hash = hashlib.sha256(
        json.dumps(VERDEXFinancialData.model_json_schema(), sort_keys=True).encode()
    ).hexdigest()
    input_version = {
        "source_blocks": SOURCE_BLOCK_VERSION,
        "selection": SELECTION_VERSION,
        "model_text": MODEL_INPUT_VERSION,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "source": source_hash,
                "context": {
                    k: item.get(k)
                    for k in (
                        "company_id",
                        "company_name",
                        "fiscal_year",
                        "source_document",
                        "source_url",
                        "content_type",
                        "filename",
                    )
                },
                "input_version": input_version,
                "model": client.model,
                "endpoint": client.base_url,
                "options": getattr(client, "extra_body", {}),
                "prompt": SYSTEM_PROMPT,
                "review_with_model": review_with_model,
                "review": REVIEW_PROMPT if review_with_model else None,
                "annual_report_wrapper": annual_report_wrapper,
                "schema": schema_hash,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    directory = Path(output_dir) / item["company_id"] / fingerprint
    receipt_path = directory / "receipt.json"
    previous_response = None
    previous_calls = []
    previous_request = None
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("status") == "succeeded":
            saved = (directory / "financial.json").read_bytes()
            if hashlib.sha256(saved).hexdigest() != receipt.get("financial_sha256"):
                raise ValueError("Saved financial output failed hash verification.")
            VERDEXFinancialData.model_validate_json(saved)
            return {**receipt, "reused": True}
        if resume:
            return {
                **receipt,
                "reused": True,
                "needs_reconciliation": receipt.get("status") in {"running", "unknown"},
            }
        archive = directory / "attempts" / str(time.time_ns())
        archive.mkdir(parents=True)
        for path in directory.iterdir():
            if path.is_file():
                shutil.copy2(path, archive / path.name)
        response_path = directory / "extraction_repair.response.json"
        if not response_path.exists():
            response_path = directory / "extraction.response.json"
        if response_path.exists() and receipt.get("status") == "failed":
            previous_response = json.loads(response_path.read_text())
            previous_calls = receipt.get("calls", [])
            previous_request = json.loads(
                response_path.with_name(
                    response_path.name.replace(".response.", ".request.")
                ).read_text()
            )
    if previous_response is None and directory.parent.exists():
        for old_receipt_path in sorted(
            directory.parent.glob("*/receipt.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            if old_receipt_path == receipt_path:
                continue
            old = json.loads(old_receipt_path.read_text())
            if (
                old.get("source_sha256"),
                old.get("schema_sha256"),
                old.get("model"),
            ) != (
                source_hash,
                schema_hash,
                client.model,
            ):
                continue
            if old.get("input_version") != input_version:
                continue
            old_request_path = old_receipt_path.parent / "extraction.request.json"
            if not old_request_path.exists():
                continue
            old_request = json.loads(old_request_path.read_text())
            if old_request.get("messages", [{}])[0].get("content") != SYSTEM_PROMPT:
                continue
            if old.get("base_url") is not None:
                if old["base_url"] != client.base_url or old.get(
                    "model_options", {}
                ) != getattr(client, "extra_body", {}):
                    continue
            else:
                # Older receipts are reusable only when their original hash proves this endpoint/options.
                review_texts = [REVIEW_PROMPT]
                for review_path in old_receipt_path.parent.glob("review*.request.json"):
                    review_texts.append(
                        json.loads(review_path.read_text())["messages"][0]["content"]
                    )
                payload = {
                    "source": source_hash,
                    "context": {
                        k: item.get(k)
                        for k in (
                            "company_id",
                            "company_name",
                            "fiscal_year",
                            "source_document",
                            "source_url",
                        )
                    },
                    "input_version": "source-blocks-v3-financial-indexed-columns-v1",
                    "model": client.model,
                    "endpoint": client.base_url,
                    "options": getattr(client, "extra_body", {}),
                    "prompt": SYSTEM_PROMPT,
                    "schema": schema_hash,
                }
                if not any(
                    hashlib.sha256(
                        json.dumps(dict(payload, review=t), sort_keys=True).encode()
                    ).hexdigest()
                    == old.get("fingerprint")
                    for t in review_texts
                ):
                    continue
            for name in ("extraction_repair.response.json", "extraction.response.json"):
                old_response = old_receipt_path.parent / name
                if old_response.exists():
                    candidate_response = json.loads(old_response.read_text())
                    try:
                        completion_text(candidate_response)
                    except (ValueError, KeyError, IndexError):
                        continue
                    previous_response = candidate_response
                    previous_request = json.loads(
                        old_response.with_name(
                            old_response.name.replace(".response.", ".request.")
                        ).read_text()
                    )
                    previous_calls = [
                        dict(c, reused=True)
                        for c in old.get("calls", [])
                        if c["operation"].startswith("extraction")
                    ]
                    break
            if previous_response is not None:
                break
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {
        "status": "running",
        "company_id": item["company_id"],
        "document_id": item.get("document_id"),
        "source_sha256": source_hash,
        "source_url": item["source_url"],
        "model": client.model,
        "base_url": client.base_url,
        "model_options": getattr(client, "extra_body", {}),
        "input_version": input_version,
        "prompt_version": PROMPT_VERSION,
        "review_with_model": review_with_model,
        "annual_report_wrapper": annual_report_wrapper,
        "schema_sha256": schema_hash,
        "fingerprint": fingerprint,
        "started_at": datetime.now(UTC).isoformat(),
        "calls": [],
        "result_directory": str(directory),
    }
    save_json(receipt_path, receipt)
    started = time.monotonic()
    outstanding_call = False
    try:
        full, selected = await asyncio.to_thread(
            prepare_financial_source,
            body,
            item["content_type"],
            item.get("filename", Path(item["path"]).name),
            item["source_url"],
        )
        full = _full_source_with_wrapper_year(full, annual_report_wrapper)
        year = (
            annual_report_wrapper["fiscal_year"]
            if annual_report_wrapper is not None
            else source_fiscal_year(full)
        )
        if item.get("fiscal_year") and item["fiscal_year"] != year:
            raise ValueError("Requested fiscal year disagrees with the report cover.")
        metadata = {
            "company_id": item["company_id"],
            "company_name": item["company_name"],
            "fiscal_year": year,
            "source_document": item["source_document"],
            "source_url": item["source_url"],
        }
        receipt.update(
            fiscal_year=year,
            full_characters=sum(len(b["text"]) for b in full["blocks"]),
            selected_characters=sum(
                len(b["text"]) + len(b["context"]) for b in selected["blocks"]
            ),
            input_selection=selected["selection"],
        )
        save_json(directory / "source_blocks.json", selected)
        save_json(
            directory / "source_manifest.json",
            {
                **metadata,
                "source_sha256": source_hash,
                "input_bytes": len(body),
                "warnings": selected["warnings"],
                "is_complete_text": full["is_complete"],
                "selection": selected["selection"],
                "annual_report_wrapper": annual_report_wrapper,
            },
        )

        async def call(messages, name):
            """Record each paid attempt before validating its content."""
            nonlocal outstanding_call
            save_json(
                directory / (name + ".request.json"),
                {"model": client.model, "messages": messages},
            )
            call_started = time.monotonic()
            outstanding_call = True
            response = await client.complete(messages, operation="financial_" + name)
            save_json(directory / (name + ".response.json"), response)
            outstanding_call = False
            receipt["calls"].append(
                {
                    "operation": name,
                    "elapsed_seconds": round(time.monotonic() - call_started, 3),
                    "usage": response.get("usage", {}),
                    "model": response.get("model", client.model),
                }
            )
            save_json(receipt_path, receipt)
            return completion_text(response)

        feedback = None
        previous_content = None
        for attempt in range(2):
            if attempt == 0 and previous_response is not None:
                content = completion_text(previous_response)
                save_json(
                    directory / "extraction.request.json",
                    previous_request,
                )
                save_json(directory / "extraction.response.json", previous_response)
                receipt["calls"] = previous_calls
                receipt["replayed_extraction"] = True
            else:
                request = extraction_messages(selected, metadata)
                if feedback:
                    payload = json.loads(request[-1]["content"])
                    payload["correction"] = {
                        "previous_response": previous_content,
                        "validation_errors": feedback,
                        "instruction": "Correct these contract errors using only the supplied source. Preserve already-correct values. Return the original currency/metrics JSON shape.",
                    }
                    request[-1]["content"] = json.dumps(payload, ensure_ascii=False)
                content = await call(
                    request, "extraction" if attempt == 0 else "extraction_repair"
                )
            try:
                data, evidence = validate_financial_response(
                    content, full, selected, metadata
                )
                if annual_report_wrapper is not None:
                    evidence["fiscal_year"] = {
                        **annual_report_wrapper["wrapper_cover"],
                        "basis": annual_report_wrapper["basis"],
                        "source_role": "sec_annual_wrapper",
                        "wrapper_document_id": annual_report_wrapper[
                            "wrapper_document_id"
                        ],
                        "wrapper_sha256": annual_report_wrapper["wrapper_sha256"],
                        "wrapper_url": annual_report_wrapper["wrapper_url"],
                        "sec_accession": annual_report_wrapper["sec_accession"],
                    }
                save_json(directory / "candidate.json", data)
                save_json(directory / "evidence.json", evidence)
                checks = []
                if review_with_model:
                    review = await call(
                        review_messages(selected, metadata, data, evidence),
                        "review" if attempt == 0 else "review_repair",
                    )
                    checks = validate_review(review)
                break
            except (ValueError, TypeError, KeyError) as error:
                if outstanding_call or attempt == 1:
                    raise
                feedback, previous_content = str(error), content
                save_json(directory / "repair_reason.json", {"reason": feedback})
        save_json(
            directory / "validation.json",
            {
                "status": "passed",
                "source_checks": True,
                "reviewed_by_model": review_with_model,
                "semantic_checks": checks,
                "prompt_version": PROMPT_VERSION,
            },
        )
        save_json(directory / "financial.json", data)
        receipt["financial_sha256"] = hashlib.sha256(
            (directory / "financial.json").read_bytes()
        ).hexdigest()
        receipt["status"] = "succeeded"
    except asyncio.CancelledError:
        receipt.update(
            status="unknown" if outstanding_call else "failed",
            error="Processing interrupted; reconcile any outstanding provider call before retry.",
            finished_at=datetime.now(UTC).isoformat(),
        )
        save_json(receipt_path, receipt)
        raise
    except Exception as error:  # noqa: BLE001 - preserve every provider outcome
        receipt.update(
            status="unknown" if outstanding_call else "failed",
            error_type=type(error).__name__,
            error=str(error)[:1800],
        )
    receipt.update(
        elapsed_seconds=round(time.monotonic() - started, 3),
        finished_at=datetime.now(UTC).isoformat(),
    )
    save_json(receipt_path, receipt)
    return receipt


async def run_batch(
    items, client, output_dir, *, concurrency=4, resume=True, review_with_model=False
):
    """Keep independent calls concurrent and checkpoint each finished company immediately."""
    if concurrency < 1:
        raise ValueError("Concurrency must be positive.")
    if len({(i["company_id"], i["path"]) for i in items}) != len(items):
        raise ValueError("Batch contains duplicate company/document work.")
    output_dir = Path(output_dir)
    lock_path = output_dir / ".batch.lock"
    output_dir.mkdir(parents=True, exist_ok=True)
    # A kernel lock is released on crashes and prevents duplicate paid batch runners.
    import fcntl

    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        semaphore = asyncio.Semaphore(concurrency)
        results = []
        pause_path = output_dir / "PAUSE"
        save_json(
            output_dir / "progress.json",
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "total": len(items),
                "completed": 0,
                "concurrency": concurrency,
                "status_counts": {},
                "results": [],
                "batch_status": "running",
            },
        )

        async def work(item):
            """Limit active documents while retaining failures as individual outcomes."""
            async with semaphore:
                if pause_path.exists():
                    return
                try:
                    result = await extract_document(
                        item,
                        client,
                        output_dir,
                        resume=resume,
                        review_with_model=review_with_model,
                    )
                except Exception as error:  # noqa: BLE001 - checkpoint each report
                    result = {
                        "company_id": item["company_id"],
                        "document_id": item.get("document_id"),
                        "status": "failed",
                        "error_type": type(error).__name__,
                        "error": str(error)[:1000],
                    }
                results.append(result)
                summary = {
                    "updated_at": datetime.now(UTC).isoformat(),
                    "total": len(items),
                    "completed": len(results),
                    "concurrency": concurrency,
                    "status_counts": {
                        status: sum(r["status"] == status for r in results)
                        for status in {r["status"] for r in results}
                    },
                    "results": results,
                    "batch_status": "paused" if pause_path.exists() else "running",
                }
                save_json(output_dir / "progress.json", summary)
                print(
                    json.dumps(
                        {
                            "company_id": result["company_id"],
                            "status": result["status"],
                            "completed": len(results),
                            "total": len(items),
                        }
                    ),
                    flush=True,
                )

        await asyncio.gather(*(work(item) for item in items))
        save_json(
            output_dir / "progress.json",
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "total": len(items),
                "completed": len(results),
                "concurrency": concurrency,
                "status_counts": {
                    status: sum(r["status"] == status for r in results)
                    for status in {r["status"] for r in results}
                },
                "results": results,
                "batch_status": "finished" if len(results) == len(items) else "paused",
            },
        )
        return results


def main():
    """Run a source manifest with any OpenAI-compatible endpoint and API key."""
    load_dotenv(Path(__file__).parents[2] / ".env")
    parser = argparse.ArgumentParser(
        description="Extract standardized financial metrics from saved annual reports."
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--manifest", type=Path)
    inputs.add_argument("--from-catalog", action="store_true")
    parser.add_argument(
        "--write-manifest",
        type=Path,
        help="Prepare source selection and exit without model calls.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/financial_extractions")
    )
    parser.add_argument("--base-url", default=os.getenv("GREEN500_LLM_BASE_URL"))
    parser.add_argument("--model", default=os.getenv("GREEN500_LLM_MODEL"))
    parser.add_argument("--api-key-env", default="GREEN500_LLM_API_KEY")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--extra-body", type=json.loads, default={})
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Allow explicit reruns including calls with unknown prior outcomes.",
    )
    parser.add_argument(
        "--review-with-model",
        action="store_true",
        help="Run a second model call that reviews the source-validated extraction.",
    )
    args = parser.parse_args()
    if args.from_catalog:
        from green500.config import load_settings

        items, missing = annual_report_manifest(load_settings())
        save_json(args.output_dir / "annual_filing_gaps.json", missing)
    else:
        items = json.loads(args.manifest.read_text())
    if not isinstance(items, list):
        raise TypeError("Source manifest must be a JSON list.")
    if args.write_manifest:
        save_json(args.write_manifest, items)
        print(
            json.dumps(
                {"annual_filings": len(items), "manifest": str(args.write_manifest)}
            )
        )
        return 0
    client = ChatCompletionClient(
        args.base_url or "",
        os.getenv(args.api_key_env, ""),
        args.model or "",
        extra_body=args.extra_body,
    )

    async def execute():
        async with client:
            return await run_batch(
                items,
                client,
                args.output_dir,
                concurrency=args.concurrency,
                resume=not args.retry_failed,
                review_with_model=args.review_with_model,
            )

    results = asyncio.run(execute())
    return int(any(r["status"] != "succeeded" for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
