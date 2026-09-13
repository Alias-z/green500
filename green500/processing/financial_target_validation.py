"""Deterministic semantic checks for fixed financial-target outputs."""

import copy
import html
import re

from green500.processing.financial_target_features import FIELDS
from green500.processing.source_quotes import normalize_source_text

_FIELD = re.compile(
    r"^financial_target_(?P<target>.+)_(?P<horizon>annual|long_term)_"
    r"(?P<attribute>value|min|max|target_year|baseline_year|is_revised|is_withdrawn)$"
)
_LOWER_BOUND = re.compile(
    r"(?:≥|>=|(?<![<])>(?!=)|\bat least\b|\bmore than\b|"
    r"\bgreater than(?: or equal to)?\b|\bminimum of\b|\d[\d,.]*\s*%?\s*\+)",
    re.IGNORECASE,
)
_UPPER_BOUND = re.compile(
    r"(?:≤|<=|(?<![>])<(?!=)|\bat most\b|\bup to\b|\bno more than\b|"
    r"\bless than(?: or equal to)?\b|\bmaximum of\b)",
    re.IGNORECASE,
)
_EXPLICIT_USD = re.compile(
    r"(?:\bU\.?S\.?\s+dollars?\b|\bUnited States dollars?\b|"
    r"(?<![/\w-])USD(?![/\w-])|\bUS\s*\$|\$\s*US\b)",
    re.IGNORECASE,
)
_TARGET_LABELS = {
    "revenue_growth": r"(?:revenue|sales|base fee)\s+(?:growth|cagr)",
    "operating_margin": r"operating\s+(?:income\s+)?margin",
    "eps_growth": r"(?:eps|earnings per share)\s+(?:growth|cagr)",
}
_TARGET_TERMS = re.compile(
    r"(?:guidance|outlook|target|ambition|aspiration|forecast|expect|project|"
    r"full[- ]year|annual|long[- ]term|multi[- ]year|through[- ](?:the[- ])?cycle|"
    r"revenue growth|sales growth|operating margin|earnings per share growth|eps growth)",
    re.IGNORECASE,
)


def _proof_text(metadata, blocks):
    """Return grounded evidence; model-authored source_unit is not evidence."""
    proof = (metadata or {}).get("evidence") or {}
    block = blocks.get(proof.get("block_id")) or {}
    return "\n".join(
        str(part or "") for part in (proof.get("quote"), block.get("text")) if part
    )


def _localized_proof_text(metadata, blocks, radius=800):
    """Return the grounded quote and its nearby original-source context."""
    proof = (metadata or {}).get("evidence") or {}
    block = blocks.get(proof.get("block_id")) or {}
    quote = normalize_source_text(proof.get("quote") or "")
    source = normalize_source_text(block.get("text") or "")
    position = source.find(quote) if quote else -1
    if position < 0:
        raw = normalize_source_text(proof.get("raw_value") or "")
        position = source.find(raw) if raw else -1
    if position < 0:
        return quote
    return quote + "\n" + source[max(0, position - radius) : position + len(quote) + radius]


def _omit(result, field, reason, audit):
    """Replace one unsupported value with an explicit local-validation omission."""
    metadata = result["metadata"].get(field) or {}
    result["values"][field] = None
    result["metadata"][field] = {
        "status": "not_extracted",
        "reporting_year": metadata.get("reporting_year"),
        "reason": reason,
        "qualification": metadata.get("qualification"),
        "confidence": None,
        "evidence": None,
    }
    audit.append({"field": field, "action": "omitted", "reason": reason})


def _move_bound(result, source_field, destination_field, reason, audit):
    """Move a comparison from point/wrong bound to its source-supported bound."""
    source_value = result["values"][source_field]
    destination_value = result["values"][destination_field]
    if destination_value is not None and destination_value != source_value:
        _omit(result, source_field, "The comparison conflicts with an existing bound.", audit)
        _omit(result, destination_field, "The comparison conflicts with an existing bound.", audit)
        return
    result["values"][destination_field] = source_value
    result["metadata"][destination_field] = result["metadata"][source_field]
    result["values"][source_field] = None
    result["metadata"][source_field] = None
    audit.append(
        {
            "field": source_field,
            "action": "moved",
            "destination_field": destination_field,
            "reason": reason,
        }
    )


def _normalize_bounds(result, blocks, audit):
    for field in tuple(result["values"]):
        match = _FIELD.match(field)
        if not match or match["attribute"] not in {"value", "min", "max"}:
            continue
        if result["values"][field] is None:
            continue
        metadata = result["metadata"].get(field) or {}
        text = str((metadata.get("evidence") or {}).get("quote") or "")
        lower = bool(_LOWER_BOUND.search(text))
        upper = bool(_UPPER_BOUND.search(text))
        if lower == upper:
            continue
        destination = field.rsplit("_", 1)[0] + ("_min" if lower else "_max")
        if field != destination:
            _move_bound(
                result,
                field,
                destination,
                "Explicit lower bound" if lower else "Explicit upper bound",
                audit,
            )


def _duplicate_point_endpoints(result, audit):
    """Represent one explicit point target as equal lower and upper endpoints."""
    for minimum_field in (field for field in FIELDS if field.endswith("_min")):
        maximum_field = minimum_field.removesuffix("_min") + "_max"
        minimum = result["values"][minimum_field]
        maximum = result["values"][maximum_field]
        if (minimum is None) == (maximum is None):
            continue
        source_field = minimum_field if minimum is not None else maximum_field
        destination_field = maximum_field if minimum is not None else minimum_field
        quote = str(
            ((result["metadata"].get(source_field) or {}).get("evidence") or {}).get(
                "quote"
            )
            or ""
        )
        if _LOWER_BOUND.search(quote) or _UPPER_BOUND.search(quote):
            continue
        result["values"][destination_field] = result["values"][source_field]
        result["metadata"][destination_field] = copy.deepcopy(
            result["metadata"][source_field]
        )
        audit.append(
            {
                "field": source_field,
                "action": "duplicated_point_endpoint",
                "destination_field": destination_field,
            }
        )


def _validate_money_units(result, blocks, audit):
    for field, value in tuple(result["values"].items()):
        if value is None or FIELDS[field]["unit"] not in {"USD", "USD_per_share"}:
            continue
        proof = (result["metadata"].get(field) or {}).get("evidence") or {}
        # Only the grounded quote can establish currency. source_unit is model output.
        if not _EXPLICIT_USD.search(str(proof.get("quote") or "")):
            _omit(
                result,
                field,
                "The grounded source quote does not explicitly identify USD.",
                audit,
            )


def _validate_boolean_claims(result, blocks, audit):
    for field, value in tuple(result["values"].items()):
        match = _FIELD.match(field)
        if not match or value is None:
            continue
        attribute = match["attribute"]
        if attribute not in {"is_revised", "is_withdrawn"}:
            continue
        text = _localized_proof_text(result["metadata"].get(field), blocks, radius=250)
        if attribute == "is_revised":
            supported = (
                bool(re.search(r"\b(revis|updat|increas|decreas|rais|lower|narrow)", text, re.IGNORECASE))
                if value
                else bool(re.search(r"\b(?:unchanged|no change|no difference|reaffirmed)\b|\bNC\b", text, re.IGNORECASE))
            )
        else:
            supported = (
                bool(re.search(r"\b(?:withdraw|rescind|cancel|discontinu)", text, re.IGNORECASE))
                if value
                else bool(re.search(r"\b(?:not withdrawn|remains active|still active|continues? in effect)\b", text, re.IGNORECASE))
            )
        if not supported:
            _omit(
                result,
                field,
                f"The grounded source does not explicitly support {attribute}={str(value).lower()}.",
                audit,
            )


def _validate_margin_levels(result, blocks, audit):
    for field, value in tuple(result["values"].items()):
        match = _FIELD.match(field)
        if not match or match["target"] != "operating_margin" or value is None:
            continue
        if match["attribute"] not in {"value", "min", "max"}:
            continue
        text = _localized_proof_text(
            result["metadata"].get(field), blocks, radius=0
        )
        if re.search(r"margin expansion|\bbps\b|basis points?", text, re.IGNORECASE):
            _omit(
                result,
                field,
                "A margin expansion or basis-point change cannot populate an operating-margin level.",
                audit,
            )


def _validate_year_links(result, blocks, audit):
    for field, value in tuple(result["values"].items()):
        match = _FIELD.match(field)
        if not match or match["attribute"] not in {"target_year", "baseline_year"} or value is None:
            continue
        text = _localized_proof_text(result["metadata"].get(field), blocks)
        unsupported_baseline = match["attribute"] == "baseline_year" and not re.search(
            r"\b(?:baseline|base year|compared (?:with|to)|from\s+20\d{2})\b",
            text,
            re.IGNORECASE,
        )
        margin_change_only = match["target"] == "operating_margin" and re.search(
            r"margin expansion|\bbps\b|basis points?", text, re.IGNORECASE
        )
        if unsupported_baseline or margin_change_only or not re.search(
            _TARGET_LABELS[match["target"]], text, re.IGNORECASE
        ):
            _omit(
                result,
                field,
                "The cited year evidence does not name the matching financial target type.",
                audit,
            )


def normalize_financial_target_result(result, evidence):
    """Return a copy with deterministic target semantics and an audit trail."""
    normalized = copy.deepcopy(result)
    blocks = {block["block_id"]: block for block in evidence["blocks"]}
    audit = []
    _normalize_bounds(normalized, blocks, audit)
    _duplicate_point_endpoints(normalized, audit)
    _validate_money_units(normalized, blocks, audit)
    _validate_boolean_claims(normalized, blocks, audit)
    _validate_margin_levels(normalized, blocks, audit)
    _validate_year_links(normalized, blocks, audit)
    return normalized, audit


def financial_target_source_hints(evidence, max_count=32, max_chars=16_000):
    """Select bounded source facts without guessing normalized target values."""
    candidates = []
    for block in evidence["blocks"]:
        lines = [" ".join(line.split()) for line in block["text"].splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if not _TARGET_TERMS.search(line) or not re.search(r"\d|≥|≤|>|<", line):
                continue
            context_lines = lines[max(0, index - 2) : min(len(lines), index + 2)]
            excerpt = "\n".join(context_lines)[:1_200]
            semantic_line = html.unescape(
                re.sub(r"<[^>]+>", " | ", line)
            )
            semantic_line = re.sub(r"\s*\|\s*", " | ", semantic_line).strip(" |")
            row_match = re.search(r'data-row-id=["\']([^"\']+)', line)
            target_types = tuple(
                target
                for target, pattern in _TARGET_LABELS.items()
                if re.search(pattern, semantic_line, re.IGNORECASE)
            )
            if not target_types:
                continue
            period_context = [
                value
                for value in context_lines
                if re.search(
                    r"20\d{2}|full[- ]year|annual|quarter|long[- ]term|"
                    r"multi[- ]year|through[- ](?:the[- ])?cycle|ambition|aspiration",
                    value,
                    re.IGNORECASE,
                )
            ]
            scope_context = [
                value
                for value in context_lines
                if re.search(
                    r"\b(?:consolidated|total company|corporation|company|segment|division)\b",
                    value,
                    re.IGNORECASE,
                )
            ]
            raw_tokens = re.findall(
                r"(?:[<>≥≤]\s*)?(?:[$€£¥]\s*)?\(?\d[\d,.]*\)?"
                r"(?:\s*(?:%|\+|bps|basis points?|million|billion|x))?",
                semantic_line,
                re.IGNORECASE,
            )
            score = 4 * bool(re.search(r"guidance|target|ambition|aspiration|forecast", excerpt, re.IGNORECASE))
            score += 3 * bool(
                re.search(
                    r"≥|≤|>|<|\d\s*%\+|\d[\d,.]*\s*%?\s*(?:to|–|-)\s*[$€£¥]?\s*\d",
                    excerpt,
                )
            )
            score += 2 * bool(re.search(r"20\d{2}", excerpt))
            score += bool(re.search(r"[$€£¥]\s*[~≈]?\d|\d\s*(?:million|billion)", excerpt, re.IGNORECASE))
            candidates.append(
                (
                    score,
                    block["block_id"],
                    index,
                    target_types,
                    {
                        "block_id": block["block_id"],
                        "row_id": row_match.group(1) if row_match else None,
                        "locator": block["locator"],
                        "candidate_target_types": list(target_types),
                        "source_text": semantic_line,
                        "source_excerpt": excerpt,
                        "period_context": period_context,
                        "scope_context": scope_context,
                        "raw_value_tokens": raw_tokens,
                        "normalization": "source_whitespace_collapsed",
                    },
                )
            )
    selected, seen, used = [], set(), 0

    def add(row):
        nonlocal used
        _, block_id, _, _, candidate = row
        key = (block_id, candidate["source_excerpt"])
        encoded = len(str(candidate))
        if key in seen or len(selected) >= max_count or used + encoded > max_chars:
            return False
        seen.add(key)
        selected.append(candidate)
        used += encoded
        return True

    ranked = sorted(candidates, key=lambda row: (-row[0], row[1], row[2]))
    for target in _TARGET_LABELS:
        selected_blocks = set()
        for row in ranked:
            if target not in row[3] or row[1] in selected_blocks:
                continue
            if add(row):
                selected_blocks.add(row[1])
            if len(selected_blocks) == 2:
                break
    for row in ranked:
        add(row)
    return selected
