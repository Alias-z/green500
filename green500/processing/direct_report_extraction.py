"""Publish fixed features found directly in targeted original-source excerpts."""

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from green500.documents import html_lines, pdf_lines
from green500.processing.feature_units import convert
from green500.processing.files import save_json
from green500.processing.fixed_report_batch import (
    PROFILES,
    _parse_fixed_raw_number,
    category_profile,
)
from green500.processing.source_quotes import normalize_source_text

VERSION = "direct-source-extraction-v1"


def load_direct_source(item):
    """Read every original page or HTML row without layout-positioned PDF text."""
    path = Path(item["path"])
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if digest != item["sha256"]:
        raise ValueError("Saved original differs from the source manifest.")
    if body.lstrip().startswith(b"%PDF-"):
        parsed = pdf_lines(body)
        pages = {}
        for line in parsed["lines"]:
            pages.setdefault(line["page"], []).append(line["text"])
        blocks = [
            {
                "block_id": page,
                "location": f"PDF page {page}",
                "text": "\n".join(lines),
            }
            for page, lines in sorted(pages.items())
        ]
    else:
        parsed = html_lines(body)
        blocks = [
            {
                "block_id": index,
                "location": line["id"],
                "text": line["text"],
            }
            for index, line in enumerate(parsed["lines"], 1)
        ]
    return {
        "version": VERSION,
        "source_sha256": digest,
        "source_url": item["source_url"],
        "source_document": item["source_document"],
        "blocks": blocks,
        "warnings": parsed["warnings"],
        "is_complete": parsed["is_complete"],
    }


def search_direct_source(source, terms, *, context_line_count=2):
    """Return bounded exact lines around case-insensitive search terms."""
    patterns = [re.compile(term, re.IGNORECASE) for term in terms]
    matches = []
    for block in source["blocks"]:
        lines = block["text"].splitlines()
        for index, line in enumerate(lines):
            if not any(pattern.search(line) for pattern in patterns):
                continue
            start = max(0, index - context_line_count)
            end = min(len(lines), index + context_line_count + 1)
            matches.append(
                {
                    "block_id": block["block_id"],
                    "location": block["location"],
                    "quote": "\n".join(lines[start:end]),
                }
            )
    return matches


def _validate_direct_fact(field, fact, blocks, profile):
    """Validate one interpreted fact against an exact original-source block."""
    if field not in profile.FIELDS:
        raise ValueError("Direct fact uses an unsupported fixed field: " + field)
    block = blocks.get(fact["block_id"])
    if block is None:
        raise ValueError(field + ": source block is missing.")
    quote = fact["quote"].strip()
    if not quote or normalize_source_text(quote) not in normalize_source_text(
        block["text"]
    ):
        raise ValueError(field + ": exact source quote is absent from its block.")
    value = fact["value"]
    specification = profile.FIELDS[field]
    expected_type = specification["type"]
    if expected_type == "boolean":
        if not isinstance(value, bool):
            raise TypeError(field + ": expected a boolean value.")
    else:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise TypeError(field + ": expected a finite numeric value.")
        raw_value = fact.get("raw_value")
        source_unit = fact.get("source_unit")
        scale_factor = fact.get("scale_factor")
        raw_number = _parse_fixed_raw_number(raw_value)
        if normalize_source_text(raw_value).replace(
            ",", ""
        ) not in normalize_source_text(quote).replace(",", ""):
            raise ValueError(field + ": copied raw value is absent from the quote.")
        if specification["unit"] in {
            "rate per 200000 hours",
            "rate per 1000000 hours",
        }:
            if source_unit != specification["unit"]:
                raise ValueError(field + ": injury-rate denominator is not exact.")
            denominator = specification["unit"].split()[2]
            if denominator not in re.sub(r"[,.\s]", "", quote):
                raise ValueError(
                    field + ": injury-rate denominator is absent from the quote."
                )
            converted = raw_number
        else:
            converted = convert(raw_number, source_unit, specification["unit"])
        if (
            field.endswith("_change_pct")
            and scale_factor == -1
            and converted is not None
        ):
            converted = -abs(converted)
        if converted is None or not math.isclose(
            converted, value, rel_tol=0.00001, abs_tol=0.000001
        ):
            raise ValueError(
                field + ": source unit does not produce the canonical value."
            )
        if not isinstance(scale_factor, (int, float)) or not math.isclose(
            raw_number * scale_factor, value, rel_tol=0.00001, abs_tol=0.000001
        ):
            raise ValueError(
                field + ": conversion scale disagrees with the canonical value."
            )
    return {
        "status": fact.get(
            "status", "company_target" if "target_" in field else "reported"
        ),
        "reporting_year": fact.get("reporting_year"),
        "reason": None,
        "qualification": fact.get("qualification"),
        "confidence": fact.get("confidence", 1.0),
        "evidence": {
            "block_id": fact["block_id"],
            "quote": quote,
            "raw_value": fact.get("raw_value"),
            "source_unit": fact.get("source_unit"),
            "scale_factor": fact.get("scale_factor"),
        },
    }


def publish_direct_extraction(
    item,
    category,
    facts,
    output_root,
    *,
    reporting_year,
    boundary=None,
    limitations=None,
    reviewed_by,
):
    """Publish a complete fixed record after direct source and schema validation."""
    if category not in PROFILES or item["category"] != category:
        raise ValueError(
            "Direct extraction category does not match its source manifest."
        )
    profile = category_profile(category)
    source = load_direct_source(item)
    blocks = {block["block_id"]: block for block in source["blocks"]}
    values = dict.fromkeys(profile.FIELDS)
    metadata = {
        field: {
            "status": "not_disclosed",
            "reporting_year": reporting_year,
            "reason": "No source-supported value was identified during targeted source review.",
            "qualification": None,
            "confidence": None,
            "evidence": None,
        }
        for field in profile.FIELDS
    }
    citations = {}
    for field, fact in facts.items():
        metadata[field] = _validate_direct_fact(field, fact, blocks, profile)
        values[field] = fact["value"]
        citations[field] = {
            **metadata[field]["evidence"],
            "location": blocks[fact["block_id"]]["location"],
        }
    data = {
        "company": {"name": item["company_name"], "ticker": item["company_id"]},
        "reporting_year": reporting_year,
        "boundary": boundary,
        "limitations": limitations or [],
        "values": values,
        "metadata": metadata,
    }
    data = profile.MODEL.model_validate(data).model_dump(mode="json")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "category": category,
                "source_sha256": item["sha256"],
                "reviewed_by": reviewed_by,
                "data": data,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    directory = (
        Path(output_root) / category / "direct" / item["company_id"] / fingerprint
    )
    directory.mkdir(parents=True, exist_ok=True)
    save_json(directory / "data.json", data)
    save_json(
        directory / "evidence.json",
        {
            "source_sha256": item["sha256"],
            "source_url": item["source_url"],
            "citations": citations,
        },
    )
    save_json(
        directory / "validation.json",
        {
            "status": "passed",
            "schema_checks": True,
            "source_identity_checks": True,
            "direct_source_checks": True,
            "reviewed_by": reviewed_by,
            "populated_fields": len(facts),
        },
    )
    save_json(
        directory / "source_manifest.json",
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
    receipt = {
        "version": VERSION,
        "direct_source_extraction": True,
        "category": category,
        "company_id": item["company_id"],
        "company_cik": item["company_cik"],
        "document_id": item["document_id"],
        "source_sha256": item["sha256"],
        "source_url": item["source_url"],
        "model": None,
        "reviewed_by": reviewed_by,
        "status": "succeeded",
        "populated_fields": len(facts),
        "processed_at": datetime.now(UTC).isoformat(),
        "output_sha256": hashlib.sha256(
            (directory / "data.json").read_bytes()
        ).hexdigest(),
    }
    receipt["started_at"] = receipt["processed_at"]
    receipt["finished_at"] = receipt["processed_at"]
    save_json(directory / "receipt.json", receipt)
    return {"directory": str(directory), "receipt": receipt, "data": data}
