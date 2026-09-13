"""Re-extract original reports into fixed, versioned company feature records."""

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from green500.processing.chat_completion import ChatCompletionClient, completion_text
from green500.processing.climate_validation import (
    climate_source_hints,
    invalid_climate_target_families,
    validate_climate_field,
)
from green500.processing.document_evidence import parse_reported_number, prepare_report
from green500.processing.feature_units import convert
from green500.processing.files import save_json
from green500.processing.financial_target_validation import (
    financial_target_source_hints,
    normalize_financial_target_result,
)
from green500.processing.source_quotes import (
    find_source_numeric_token,
    ground_source_quote,
    normalize_source_text,
)
from green500.processing.source_semantics import validate_scope_field

VERSION = "fixed-original-report-v1"
PROFILES = {
    "environment_report": "fixed_environment_profile",
    "social_employee": "fixed_social_profile",
    "climate_targets": "fixed_climate_profile",
    "financial_targets": "fixed_financial_targets_profile",
}

_WRITTEN_NUMBERS = {"zero": 0.0, "one": 1.0}

_SOCIAL_FIELD_LABELS = {
    "social_employees_count": (
        "total workforce",
        "employee population",
        "number of employees",
    ),
    "social_employee_turnover_percent": ("turnover rate", "employee turnover"),
    "social_women_workforce_percent": ("women in the workforce", "women represented"),
    "social_employee_fatalities_count": (
        "employee fatalities",
        "employees fatalities",
        "fatalities",
    ),
    "social_contractor_fatalities_count": (
        "contractor fatalities",
        "contractors fatalities",
        "fatalities",
    ),
    "social_recordable_injury_rate_per_200000_hours": (
        "recordable injury rate",
        "recordable rate",
        "200,000 hours",
    ),
    "social_recordable_injury_rate_per_1000000_hours": (
        "recordable injury rate",
        "1,000,000 hours",
    ),
    "social_suppliers_audited_count": ("suppliers audited", "supplier audits"),
    "social_confirmed_violations_count": (
        "confirmed violations",
        "human rights violations",
    ),
    "social_community_investment_usd": (
        "community investment",
        "community projects",
        "charitable contributions",
        "philanthropic funding",
        "in donations",
    ),
}

_SOCIAL_BALANCED_TERM_GROUPS = (
    ("total workforce", "number of employees", "employee population", "employees"),
    ("turnover rate", "employee turnover"),
    ("women in the workforce", "women workforce", "% women", "female employees"),
    ("employee fatalities", "fatalities"),
    ("contractor fatalities",),
    ("recordable injury rate", "200,000 hours", "1,000,000 hours"),
    ("suppliers audited", "supplier audits"),
    ("confirmed violations", "human rights violations"),
    ("community investment", "charitable contributions"),
)


def _parse_fixed_raw_number(raw_value):
    """Parse one source number while preserving explicit approximation wording."""
    if not isinstance(raw_value, str):
        raise TypeError("The raw value must be source text.")
    stripped = raw_value.strip()
    fiscal_year = re.fullmatch(r"FY\s*['’]?(\d{2}|\d{4})", stripped, re.IGNORECASE)
    if fiscal_year:
        year = int(fiscal_year.group(1))
        return float(2000 + year if year < 100 else year)
    written = _WRITTEN_NUMBERS.get(stripped.casefold())
    if written is not None:
        return written
    stripped = re.sub(
        r"^(?:[~≈><≤≥]|approximately\b|approx\.?\b|about\b|around\b|roughly\b|"
        r"nearly\b|over\b|more\s+than\b|at\s+least\b)\s*",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    return parse_reported_number(stripped)


def _raw_value_multiplier(raw_value):
    """Return a magnitude written next to the copied number, without inferring one."""
    text = str(raw_value or "").strip().casefold()
    if re.search(r"(?:\bmillion\b|\d\s*m\b)", text):
        return 1_000_000.0
    if re.search(r"(?:\bbillion\b|\d\s*bn\b)", text):
        return 1_000_000_000.0
    if re.search(r"(?:\bthousand\b|\d\s*k\b)", text):
        return 1_000.0
    return 1.0


def _canonical_conversion_factor(source_unit, target_unit, raw_value):
    """Derive one conversion factor from explicit unit and raw-value magnitudes."""
    unit_factor = convert(1.0, source_unit, target_unit)
    if unit_factor is None:
        return None
    raw_factor = _raw_value_multiplier(raw_value)
    if raw_factor == 1:
        return unit_factor
    if unit_factor == 1:
        return raw_factor
    if math.isclose(unit_factor, raw_factor, rel_tol=0.00001, abs_tol=0.000001):
        return unit_factor
    return None


def _validate_environment_energy_boundary(field, source_quote, qualification):
    """Reject energy subsets that do not establish the requested company metric."""
    context = " ".join(
        str(part or "") for part in (source_quote, qualification)
    ).casefold()
    if field == "env_total_energy_mwh" and (
        "office energy" in context and "total energy" not in context
    ):
        raise ValueError("Office energy cannot substitute for total energy.")
    if field == "env_renewable_energy_mwh" and (
        "renewable electricity" in context and "renewable energy" not in context
    ):
        raise ValueError(
            "Renewable electricity cannot substitute for total renewable energy."
        )
    if field != "env_renewable_electricity_mwh":
        return
    explicitly_total_electricity = any(
        phrase in context
        for phrase in (
            "total renewable electricity",
            "global renewable electricity",
            "renewable electricity consumed",
            "renewable electricity consumption",
        )
    )
    if "renewable energy" in context and "renewable electricity" not in context:
        raise ValueError(
            "Total renewable energy cannot substitute for renewable electricity."
        )
    if not explicitly_total_electricity and (
        re.search(r"\bon[ -]?site\b", context)
        or "purchased or acquired renewable electricity" in context
        or "acquired renewable electricity" in context
        or "purchased renewable electricity" in context
        or "renewable energy certificate" in context
        or "renewable electricity certificate" in context
        or re.search(r"\brec(?:s)?\b", context)
    ):
        raise ValueError(
            "An onsite or certificate subset cannot substitute for total renewable electricity."
        )


def _validate_environment_field_meaning(field, source_quote, raw_value):
    """Reject common scope and bound mismatches using the cited source row."""
    context = normalize_source_text(source_quote)
    raw = re.escape(normalize_source_text(raw_value or ""))
    if raw and re.search(
        rf"(?:[<>≤≥]\s*{raw}|(?:at least|at most|more than|less than|greater than)\s*{raw})",
        context,
    ):
        raise ValueError("A bounded disclosure cannot populate a point-value field.")
    if field == "env_scope_3_total_tco2e":
        if "scope 3" not in context:
            raise ValueError("An overall footprint cannot substitute for Scope 3.")
        if re.search(r"scope 3\s*\([^)]*categor", context):
            raise ValueError(
                "Selected Scope 3 categories are not a complete Scope 3 inventory."
            )
    if field == "env_total_ghg_tco2e" and not any(
        phrase in context
        for phrase in ("overall", "total greenhouse", "total ghg", "carbon emissions")
    ):
        raise ValueError(
            "The cited row does not state an overall greenhouse-gas total."
        )


def _source_quote_contains_raw_value(source_quote, raw_value):
    """Match a copied raw value while ignoring numeric grouping commas."""
    quote = normalize_source_text(source_quote).replace(",", "")
    raw = normalize_source_text(raw_value).replace(",", "")
    return bool(raw and re.search(r"(?<![\d.])" + re.escape(raw) + r"(?![\d.])", quote))


def _social_candidate_excerpts(evidence, max_chars=10_000):
    """Bring generic social rows to the prompt tail without inserting values."""
    candidates = []
    used_chars = 0
    for field, labels in _SOCIAL_FIELD_LABELS.items():
        field_candidates = []
        for block in evidence["blocks"]:
            source_lines = [
                " ".join(line.split())
                for line in block["text"].splitlines()
                if line.strip()
            ]
            for line_index, source_line in enumerate(source_lines):
                searchable_line = source_line.casefold()
                positions = [searchable_line.find(label) for label in labels]
                positions = [position for position in positions if position >= 0]
                if not positions:
                    continue
                text = "\n".join(
                    source_lines[
                        max(0, line_index - 12) : min(len(source_lines), line_index + 4)
                    ]
                )
                if len(text) > 1_200:
                    position = min(
                        text.casefold().find(label)
                        for label in labels
                        if text.casefold().find(label) >= 0
                    )
                    start = max(0, position - 300)
                    text = text[start : start + 1_200]
                locator = block["locator"]
                score = int(bool(re.search(r"\d", text)))
                if "2025" in text:
                    score += 2
                if field == "social_community_investment_usd" and (
                    "$" in text or "usd" in text.casefold()
                ):
                    score += 4
                field_candidates.append(
                    (
                        score,
                        block["block_id"],
                        {
                            "field": field,
                            "block_id": block["block_id"],
                            "original_block_id": locator["original_block_id"],
                            "page": locator.get("page"),
                            "source_excerpt": text,
                            "normalization": "source_whitespace_collapsed",
                        },
                    )
                )
        for _, _, candidate in sorted(
            field_candidates, key=lambda row: (-row[0], row[1])
        )[:1]:
            if used_chars >= max_chars:
                break
            encoded_length = len(json.dumps(candidate, ensure_ascii=False))
            if used_chars + encoded_length > max_chars:
                break
            candidates.append(candidate)
            used_chars += encoded_length
    return candidates


def category_profile(category):
    return importlib.import_module("green500.processing." + PROFILES[category])


def available_report_manifest(settings, category):
    """Select the best saved originals without consulting old AI results."""
    from green500 import db
    from green500.catalog import (
        SOURCE_QUERY,
        category_freshness_status,
        describe_source,
    )

    category_profile(category)
    with db.connect(settings) as connection:
        companies = connection.execute(
            "SELECT cik,name,symbols FROM companies WHERE is_current ORDER BY name,cik"
        ).fetchall()
        sources = connection.execute(SOURCE_QUERY, (None, None)).fetchall()
    choices = {}
    for raw in sources:
        if not raw.get("document_id") or category not in raw["categories"]:
            continue
        source = describe_source(dict(raw))
        freshness_status = category_freshness_status(source, category)
        order = (
            {"latest_verified": 3, "unknown": 2, "newer_available": 1}.get(
                freshness_status, 0
            ),
            str(source["freshness"].get("publication_date") or ""),
            "pdf" in (source.get("content_type") or ""),
            source["document_id"],
        )
        if (
            source["company_cik"] not in choices
            or order > choices[source["company_cik"]][0]
        ):
            choices[source["company_cik"]] = order, source
    result = []
    for company in companies:
        if company["cik"] not in choices:
            continue
        source = choices[company["cik"]][1]
        result.append(
            {
                "category": category,
                "company_id": company["symbols"][0]
                if company["symbols"]
                else company["cik"],
                "company_cik": company["cik"],
                "company_name": company["name"],
                "document_id": source["document_id"],
                "source_id": source["id"],
                "sha256": source["sha256"],
                "source_document": source["title"],
                "source_url": source["url"],
                "content_type": source["content_type"],
                "filename": source["url"].split("?", 1)[0].rsplit("/", 1)[-1],
                "path": str(
                    settings.data_dir
                    / "objects"
                    / source["sha256"][:2]
                    / source["sha256"]
                ),
                "source_review": source["freshness"],
            }
        )
    return result


def extraction_messages(item, evidence, profile):
    user_content = {
        "company": {"name": item["company_name"], "ticker": item["company_id"]},
        "source_url": item["source_url"],
        "json_schema": profile.MODEL.model_json_schema(),
        "document_evidence": evidence,
    }
    if item["category"] == "social_employee":
        user_content["social_field_candidate_excerpts"] = _social_candidate_excerpts(
            evidence, max_chars=5_000
        )
    if item["category"] == "climate_targets":
        user_content["climate_source_hints"] = climate_source_hints(evidence)
    if item["category"] == "financial_targets":
        user_content["financial_target_source_hints"] = financial_target_source_hints(
            evidence
        )
    return [
        {
            "role": "system",
            "content": profile.INSTRUCTIONS
            + "\nReturn JSON with every required fixed key. Use null metadata for null values to keep output short. Never read or reuse previous AI output. Copy the provided block_id exactly and the full labeled table row without ellipses; repeated values require their row label. Numeric evidence scale_factor is the conversion from the copied source number to the canonical unit. Keep quotes short and exact.",
        },
        {"role": "user", "content": json.dumps(user_content, ensure_ascii=False)},
    ]


def validate_result(text, item, evidence, profile):
    """Validate fixed shape; retain source-supported values and explicit omissions."""
    result = profile.MODEL.model_validate_json(text).model_dump(mode="json")
    if result["company"] != {
        "name": item["company_name"],
        "ticker": item["company_id"],
    }:
        raise ValueError("The output company must match the supplied company exactly.")
    blocks = {block["block_id"]: block for block in evidence["blocks"]}
    invalid_target_families = (
        invalid_climate_target_families(result)
        if item["category"] == "climate_targets"
        else {}
    )
    citations, omitted, semantic_adjustments = {}, [], []

    for field, value in result["values"].items():
        if value is None:
            continue
        metadata = result["metadata"][field]
        proof = metadata["evidence"]
        try:
            unit = profile.FIELDS[field]["unit"]
            if (
                item["category"] in {"environment_report", "social_employee"}
                and metadata["status"] == "company_target"
            ):
                raise ValueError(
                    "A target cannot populate an actual-performance feature."
                )
            if item["category"] == "climate_targets":
                expected_status = (
                    "company_target"
                    if field == "climate_net_zero_target_year"
                    or field.startswith("climate_target_")
                    else "reported"
                )
                if field.endswith("_progress_pct") and metadata["status"] == "reported":
                    metadata["status"] = "company_target"
                if metadata["status"] != expected_status:
                    raise ValueError(
                        f"{field} requires metadata status {expected_status}."
                    )
            if (
                item["category"] == "financial_targets"
                and metadata["status"] != "company_target"
            ):
                raise ValueError(
                    "A financial target endpoint requires company_target status."
                )
            block = blocks.get(proof["block_id"])
            if block is None:
                raise ValueError("The evidence references an unknown source block.")
            model_quote = proof["quote"]
            model_supplied_raw_value = proof.get("raw_value")
            grounding_raw_value = proof.get("raw_value")
            if (
                item["category"] == "climate_targets"
                and field.endswith("_change_pct")
                and isinstance(grounding_raw_value, str)
            ):
                grounding_raw_value = grounding_raw_value.removeprefix("-")
            proof["quote"], quote_method = ground_source_quote(
                block["text"],
                model_quote,
                None if isinstance(value, bool) else grounding_raw_value,
            )
            quote_hydrated = proof["quote"] != model_quote
            if item["category"] in {"environment_report", "climate_targets"}:
                validate_scope_field(field, proof["quote"], grounding_raw_value)
            if item["category"] == "environment_report":
                _validate_environment_energy_boundary(
                    field, proof["quote"], metadata["qualification"]
                )
                _validate_environment_field_meaning(
                    field, proof["quote"], grounding_raw_value
                )
            if item["category"] == "climate_targets":
                normalized_block = normalize_source_text(block["text"])
                normalized_quote = normalize_source_text(proof["quote"])
                quote_position = normalized_block.find(normalized_quote)
                source_context = (
                    normalized_block[
                        max(0, quote_position - 350) : quote_position
                        + len(normalized_quote)
                        + 350
                    ]
                    if quote_position >= 0
                    else model_quote
                )
                validate_climate_field(
                    field,
                    value,
                    metadata,
                    proof,
                    proof["quote"],
                    result,
                    model_quote=model_quote,
                    source_context=source_context,
                    invalid_target_families=invalid_target_families,
                )
            if not isinstance(value, bool):
                model_raw_value = model_supplied_raw_value
                proof["raw_value"] = find_source_numeric_token(
                    proof["quote"], model_raw_value
                )
                if unit == "year" and re.fullmatch(r"20\d{2}", str(proof["raw_value"])):
                    short_year = str(proof["raw_value"])[-2:]
                    fiscal_year = re.search(
                        rf"\bFY\s*['’]?{short_year}\b",
                        proof["quote"],
                        re.IGNORECASE,
                    )
                    if fiscal_year:
                        proof["raw_value"] = fiscal_year.group(0)
                raw = proof["raw_value"]
                if raw != model_raw_value:
                    citations.setdefault(field, {}).update(
                        model_raw_value=model_raw_value,
                        raw_value_correction="exact_numeric_token_from_grounded_source_quote",
                    )
                if not _source_quote_contains_raw_value(proof["quote"], raw):
                    raise ValueError(
                        "The copied number is absent from the quoted evidence."
                    )
                number = _parse_fixed_raw_number(raw)
                if "hours" in str(unit):
                    denominator = "200000" if "200000" in str(unit) else "1000000"
                    denominator_evidence = " ".join(
                        str(part or "")
                        for part in (
                            proof["source_unit"],
                            proof["quote"],
                            metadata["qualification"],
                        )
                    )
                    if denominator not in re.sub(r"[,.\s]", "", denominator_evidence):
                        raise ValueError("The injury-rate denominator is not explicit.")
                    converted = number
                else:
                    converted = number * proof["scale_factor"]
                model_declared_scale_factor = proof["scale_factor"]
                declared_scale_factor = model_declared_scale_factor
                source_meaning = " ".join(
                    str(part or "")
                    for part in (
                        proof["quote"],
                        source_context
                        if item["category"] == "climate_targets"
                        else metadata["qualification"],
                    )
                ).casefold()
                signed_decrease = (
                    field.endswith("_change_pct")
                    and declared_scale_factor == -1
                    and any(
                        term in source_meaning
                        for term in ("reduc", "decreas", "fell", "lower", "↓")
                    )
                )
                reduction_magnitude = (
                    field.endswith("_reduction_pct")
                    and number < 0
                    and value >= 0
                    and "reduc" in source_meaning
                )
                negative_progress = (
                    field.endswith("_progress_pct")
                    and number >= 0
                    and value < 0
                    and declared_scale_factor == -1
                    and any(
                        term in source_meaning
                        for term in ("increase", "above baseline", "higher")
                    )
                )
                signed_conversion = (
                    signed_decrease or reduction_magnitude or negative_progress
                )
                normalized_direction = (
                    declared_scale_factor < 0 and not signed_conversion
                )
                if normalized_direction:
                    declared_scale_factor = 1
                if declared_scale_factor == 0:
                    raise ValueError("The conversion scale cannot be zero.")
                if "hours" not in str(unit):
                    conversion_factor = _canonical_conversion_factor(
                        proof["source_unit"], unit, raw
                    )
                    if conversion_factor is None:
                        raise ValueError(
                            "The source unit and raw-value magnitude do not support the canonical unit."
                        )
                    if signed_conversion:
                        conversion_factor = -conversion_factor
                    converted = number * conversion_factor
                    proof["scale_factor"] = conversion_factor
                if profile.FIELDS[field]["type"] == "integer":
                    if not math.isclose(converted, round(converted), abs_tol=0.000001):
                        raise ValueError(
                            "The source does not support an integer count."
                        )
                    converted = round(converted)
                if not math.isclose(
                    converted, value, rel_tol=0.00001, abs_tol=0.000001
                ):
                    direction_only_correction = (
                        normalized_direction
                        and field.endswith("_progress_pct")
                        and math.isclose(
                            abs(value),
                            abs(converted),
                            rel_tol=0.00001,
                            abs_tol=0.000001,
                        )
                        and "reduction" in source_meaning
                    )
                    if not direction_only_correction and not math.isclose(
                        number, value, rel_tol=0.00001, abs_tol=0.000001
                    ):
                        raise ValueError(
                            "The proposed value matches neither the copied source number nor its canonical conversion."
                        )
                    citations.setdefault(field, {}).update(
                        model_value=value,
                        value_correction="deterministic_source_unit_conversion",
                    )
                    result["values"][field] = converted
                if not math.isclose(
                    model_declared_scale_factor,
                    proof["scale_factor"],
                    rel_tol=0.00001,
                    abs_tol=0.000001,
                ):
                    citations.setdefault(field, {}).update(
                        model_scale_factor=model_declared_scale_factor,
                        scale_correction="deterministic_source_unit_conversion",
                    )
                value = result["values"][field]
                if (
                    item["category"] in {"environment_report", "social_employee"}
                    and value < 0
                ):
                    raise ValueError(
                        "A physical actual-performance feature cannot be negative."
                    )
                if unit == "percent" and not (
                    -100 <= value <= 100
                    if field.endswith(("_change_pct", "_progress_pct"))
                    or item["category"] == "financial_targets"
                    else 0 <= value <= 100
                ):
                    raise ValueError("A percentage lies outside its supported range.")
                if unit == "year" and not 1900 <= value <= 2100:
                    raise ValueError("The year lies outside the supported range.")
            if (
                item["category"] in {"environment_report", "social_employee"}
                and metadata["reporting_year"] != result["reporting_year"]
            ):
                raise ValueError(
                    "The measurement year differs from the selected report year."
                )
            citations[field] = {
                **citations.get(field, {}),
                **proof,
                "locator": block["locator"],
            }
            if quote_hydrated:
                citations[field].update(
                    model_quote=model_quote,
                    quote_hydration=quote_method,
                )
        except (ValueError, TypeError) as error:
            result["values"][field] = None
            result["metadata"][field] = {
                "status": "not_extracted",
                "reporting_year": metadata["reporting_year"],
                "reason": str(error),
                "qualification": metadata["qualification"],
                "confidence": None,
                "evidence": None,
            }
            omitted.append({"field": field, "reason": str(error)})
    if item["category"] == "financial_targets":
        result, semantic_adjustments = normalize_financial_target_result(
            result, evidence
        )
        prior_citations = list(citations.values())
        citations = {}
        for field, value in result["values"].items():
            if value is None:
                continue
            proof = (result["metadata"].get(field) or {}).get("evidence") or {}
            matching = next(
                (
                    citation
                    for citation in prior_citations
                    if all(
                        citation.get(key) == proof.get(key)
                        for key in ("block_id", "quote", "raw_value")
                    )
                ),
                None,
            )
            if matching is not None:
                citations[field] = matching
    profile.MODEL.model_validate(result)
    validation_evidence = {
        "source_sha256": evidence["sha256"],
        "source_url": item["source_url"],
        "citations": citations,
        "omitted_fields": omitted,
    }
    if semantic_adjustments:
        validation_evidence["financial_target_semantic_adjustments"] = (
            semantic_adjustments
        )
    return result, validation_evidence


async def extract_report(item, client, output_dir, *, retry_failed=False):
    profile = category_profile(item["category"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", item["company_id"]):
        raise ValueError("Unsafe company identifier.")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "input_format": "report-evidence-v4-pdf-page-identifiers",
                "source": item["sha256"],
                "category": item["category"],
                "company": item["company_cik"],
                "model": client.model,
                "endpoint": client.base_url,
                "instructions": profile.INSTRUCTIONS,
                "schema": profile.MODEL.model_json_schema(),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    directory = Path(output_dir) / item["company_id"] / fingerprint
    receipt_path = directory / "receipt.json"
    if receipt_path.exists():
        prior = json.loads(receipt_path.read_text())
        if prior["status"] == "succeeded" or not retry_failed:
            return prior
        import shutil

        archive = directory / "attempts" / str(time.time_ns())
        archive.mkdir(parents=True)
        for path in directory.iterdir():
            if path.is_file():
                shutil.copy2(path, archive / path.name)
    receipt = {
        "category": item["category"],
        "company_id": item["company_id"],
        "company_cik": item["company_cik"],
        "document_id": item["document_id"],
        "source_sha256": item["sha256"],
        "source_url": item["source_url"],
        "model": client.model,
        "status": "running",
        "version": VERSION,
        "fresh_original_extraction": True,
        "started_at": datetime.now(UTC).isoformat(),
        "calls": [],
        "reviewed_by_model": False,
    }
    save_json(receipt_path, receipt)
    try:
        preparation = {
            "environment_report": {"max_characters": 120_000},
            "social_employee": {
                "max_characters": 29_500,
                "include_leading_blocks": False,
                "balanced_term_groups": _SOCIAL_BALANCED_TERM_GROUPS,
                "require_keyword_match": True,
            },
            "climate_targets": {
                "max_characters": 60_000,
                "include_leading_blocks": False,
            },
            "financial_targets": {
                "max_characters": 45_000,
                "include_leading_blocks": False,
            },
        }[item["category"]]
        evidence = await asyncio.to_thread(prepare_report, item, profile, **preparation)
        save_json(directory / "source_blocks.json", evidence)
        receipt["text_format_version"] = evidence.get("text_format_version")
        messages = extraction_messages(item, evidence, profile)
        for attempt in range(2):
            name = "extraction" if attempt == 0 else "correction"
            save_json(
                directory / (name + ".request.json"),
                {"model": client.model, "messages": messages},
            )
            started = time.monotonic()
            response = await client.complete(
                messages, operation="fixed_" + item["category"] + "_" + name
            )
            save_json(directory / (name + ".response.json"), response)
            receipt["calls"].append(
                {
                    "operation": name,
                    "seconds": round(time.monotonic() - started, 3),
                    "usage": response.get("usage", {}),
                }
            )
            save_json(receipt_path, receipt)
            try:
                result, citations = validate_result(
                    completion_text(response), item, evidence, profile
                )
                break
            except (ValueError, TypeError, KeyError) as error:
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
                        "content": "Correct this schema error using the original supplied source: "
                        + str(error)[:2500],
                    },
                ]
        save_json(directory / "evidence.json", citations)
        save_json(
            directory / "validation.json",
            {
                "status": "passed",
                "schema_checks": True,
                "source_identity_checks": True,
                "source_quote_checks": True,
                "reviewed_by_model": False,
                "populated_fields": len(citations["citations"]),
                "omitted_fields": len(citations["omitted_fields"]),
            },
        )
        save_json(directory / "data.json", result)
        receipt.update(
            status="succeeded",
            populated_fields=len(citations["citations"]),
            output_sha256=hashlib.sha256(
                (directory / "data.json").read_bytes()
            ).hexdigest(),
        )
    except Exception as error:  # noqa: BLE001 - every report needs a durable failed receipt
        receipt.update(
            status="failed", error_type=type(error).__name__, error=str(error)[:1500]
        )
    finally:
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        save_json(receipt_path, receipt)
    return receipt


async def run_batch(items, client, output_dir, *, concurrency=3, retry_failed=False):
    """Run disjoint company work concurrently with visible receipt checkpoints."""
    import fcntl

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".batch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        semaphore, results = asyncio.Semaphore(concurrency), []

        def progress(finished=False):
            save_json(
                output_dir / "progress.json",
                {
                    "total": len(items),
                    "completed": len(results),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "results": results,
                    "status_counts": {
                        s: sum(r["status"] == s for r in results)
                        for s in {r["status"] for r in results}
                    },
                    "batch_status": "paused"
                    if (output_dir / "PAUSE").exists()
                    else "finished"
                    if finished
                    else "running",
                },
            )

        async def work(item):
            async with semaphore:
                if (output_dir / "PAUSE").exists():
                    return
                result = await extract_report(
                    item, client, output_dir, retry_failed=retry_failed
                )
                results.append(result)
                progress()
                print(
                    json.dumps(
                        {
                            "company_id": item["company_id"],
                            "category": item["category"],
                            "status": result["status"],
                            "completed": len(results),
                            "total": len(items),
                        }
                    ),
                    flush=True,
                )

        progress()
        await asyncio.gather(*(work(item) for item in items))
        progress(finished=True)
        return results


def main():
    from green500.config import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", choices=PROFILES, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--write-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env", default="GREEN500_LLM_API_KEY")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    items = (
        json.loads(args.manifest.read_text())
        if args.manifest
        else available_report_manifest(settings, args.category)
    )
    if args.write_manifest:
        save_json(args.write_manifest, items)
        return
    client = ChatCompletionClient(
        args.base_url or settings.llm_base_url,
        os.getenv(args.api_key_env, ""),
        args.model or settings.llm_model,
        timeout_seconds=900,
        extra_body={"max_tokens": 24000},
    )

    async def execute():
        async with client:
            return await run_batch(
                items,
                client,
                args.output_dir
                or settings.data_dir / "structured_extractions" / args.category,
                concurrency=args.concurrency,
                retry_failed=args.retry_failed,
            )

    asyncio.run(execute())


if __name__ == "__main__":
    main()
