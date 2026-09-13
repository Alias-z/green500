"""Validate financial output and hydrate exact evidence from source row IDs."""

import json
import re
from decimal import Decimal, InvalidOperation

from green500.processing.financial import output_shape

_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])(?P<open>\()?\s*(?P<sign>[+-])?\s*"
    r"(?:[$€£¥]\s*)?(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<close>\))?"
)
_OUTFLOW_MAGNITUDE_FIELDS = {
    "cash_flow.property_equipment_purchases",
    "cash_flow.share_repurchases",
}
_FISCAL_YEAR_HEADER = re.compile(
    r"\bFor the fiscal year ended\s+"
    r"(?:[A-Za-z]+\s+\d{1,2},\s+)?(?P<year>(?:19|20)\d{2})\b",
    re.IGNORECASE,
)


def _is_money_field(path):
    """Return whether a financial field uses the source table's currency scale."""
    section, field = path.split(".", 1)
    if section in {"income_statement", "balance_sheet", "geography"}:
        return True
    if section == "cash_flow":
        return not field.endswith("percent")
    return section == "non_gaap" and not field.endswith("percent")


def _source_scale(block, path):
    """Return the explicit currency scale that applies to one cited block."""
    if not _is_money_field(path):
        return Decimal(1)
    source = (block.get("context", "") + "\n" + block.get("text", "")).casefold()
    if re.search(r"\bin billions?\b", source):
        return Decimal(1_000_000_000)
    if re.search(r"\bin millions?\b", source):
        return Decimal(1_000_000)
    if re.search(r"\bin thousands?\b", source):
        return Decimal(1_000)
    return Decimal(1)


def _reported_numbers(quote):
    """Return exact signed decimal quantities present in one hydrated quote."""
    values = []
    for match in _NUMBER.finditer(quote):
        try:
            value = Decimal(match.group("number").replace(",", ""))
        except InvalidOperation:
            continue
        parenthesized = bool(match.group("open") and match.group("close"))
        if parenthesized or match.group("sign") == "-":
            value = -value
        values.append(value)
    return values


def _row_supports_value(path, value, block, quote):
    """Check scale and sign without claiming semantic field correctness."""
    expected = Decimal(str(value)) / _source_scale(block, path)
    numbers = _reported_numbers(quote)
    if expected == 0:
        cells = {cell.strip() for cell in quote.split("|")}
        return Decimal(0) in numbers or bool(cells & {"—", "–", "-"})
    if path in _OUTFLOW_MAGNITUDE_FIELDS:
        return any(abs(number) == abs(expected) for number in numbers)
    return expected in numbers


def _hydrate_citation(path, citation, blocks):
    """Resolve a model-selected block and row to an exact stored source quote."""
    if not isinstance(citation, dict):
        raise TypeError(f"{path}: citation must be an object")
    block_id = citation.get("block_id")
    if not isinstance(block_id, str) or block_id not in blocks:
        raise ValueError(f"{path}: cited source block does not exist")
    if "row_id" not in citation:
        raise ValueError(f"{path}: citation must include row_id")
    block = blocks[block_id]
    row_id = citation.get("row_id")
    if block.get("kind") == "table":
        if not isinstance(row_id, str) or not row_id:
            raise ValueError(f"{path}: table citation must select one row_id")
        rows = {
            row.get("id"): row.get("text")
            for row in block.get("rows", [])
            if isinstance(row, dict)
        }
        quote = rows.get(row_id)
        if not isinstance(quote, str) or not quote:
            raise ValueError(f"{path}: cited row does not exist or is empty")
        if f'data-row-id="{row_id}"' not in block.get("text", ""):
            raise ValueError(f"{path}: cited row is not present in the table HTML")
    else:
        if row_id is not None:
            raise ValueError(f"{path}: non-table citation must use row_id null")
        quote = block.get("text")
        if not isinstance(quote, str) or not quote:
            raise ValueError(f"{path}: cited source block is empty")
    return {
        "block_id": block_id,
        "row_id": row_id,
        "quote": quote,
        "basis": "reported",
    }


def _fiscal_year_evidence(source_document, requested_fiscal_year, output_fiscal_year):
    """Verify fiscal-year identity from the request and exact full-source header."""
    if not isinstance(source_document, dict):
        raise TypeError("Full source document is required for fiscal-year validation.")
    if isinstance(requested_fiscal_year, bool) or not isinstance(
        requested_fiscal_year, int
    ):
        raise TypeError("Requested fiscal year must be an integer.")
    matches = []
    for block in source_document.get("blocks", []):
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        match = _FISCAL_YEAR_HEADER.search(text)
        if match:
            matches.append((int(match.group("year")), block))
    years = {year for year, _block in matches}
    if not matches or len(years) != 1:
        raise ValueError("Full source has no unique fiscal-year cover header.")
    source_year, block = matches[0]
    if (
        source_year != requested_fiscal_year
        or output_fiscal_year != requested_fiscal_year
    ):
        raise ValueError(
            "Output, request and full-source fiscal years must match exactly."
        )
    return {
        "block_id": block["id"],
        "row_id": None,
        "quote": block["text"],
        "basis": "deterministic_metadata",
        "source_role": "source_header",
    }


def validate_result(
    response_text,
    document,
    *,
    source_document=None,
    requested_fiscal_year=None,
):
    """Reject malformed values and hydrate every numeric citation from source IDs."""
    output = json.loads(response_text)
    if not isinstance(output, dict) or set(output) != {"data", "evidence"}:
        raise ValueError("Response must contain exactly data and evidence objects.")
    data = output["data"]
    shape = output_shape()
    if not isinstance(data, dict) or set(data) != set(shape):
        raise ValueError("Financial output sections do not match the requested schema.")
    numeric_fields = []
    for section, fields in shape.items():
        if not isinstance(data[section], dict) or set(data[section]) != set(fields):
            raise ValueError(f"Fields do not match the requested {section} schema.")
        for field, value in data[section].items():
            if value is None:
                continue
            path = f"{section}.{field}"
            if section == "company" and field != "fiscal_year":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{path} must be a nonempty string or null.")
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{path} must be a number or null.")
                if not field.endswith("percent") and not isinstance(value, int):
                    raise ValueError(f"{path} must be an integer in its base unit.")
                numeric_fields.append(path)

    if document.get("source_sha256") != (source_document or {}).get("source_sha256"):
        raise ValueError(
            "Selected and full source documents must have the same SHA-256."
        )
    fiscal_evidence = _fiscal_year_evidence(
        source_document,
        requested_fiscal_year,
        data["company"]["fiscal_year"],
    )
    required = [path for path in numeric_fields if path != "company.fiscal_year"]
    unique_fields = {}
    for path in required:
        unique_fields.setdefault(path.rsplit(".", 1)[1], []).append(path)

    raw_evidence = output["evidence"]
    if not isinstance(raw_evidence, dict):
        raise TypeError("Evidence must be an object indexed by field path.")
    references = {}
    canonicalized_bare_paths = []
    model_provided_fiscal_year_evidence = False
    for original_path, citation in raw_evidence.items():
        path = original_path.removeprefix("data.")
        if path == "company.fiscal_year":
            model_provided_fiscal_year_evidence = True
            continue
        if "." not in path:
            candidates = unique_fields.get(path, [])
            if len(candidates) != 1:
                raise ValueError(
                    f"{path}: bare evidence path is not unique in the requested schema"
                )
            path = candidates[0]
            canonicalized_bare_paths.append(original_path)
        if path in references:
            raise ValueError("Duplicate evidence paths after canonicalization.")
        if path not in required:
            raise ValueError(
                f"{path}: evidence is allowed only for non-null numeric fields"
            )
        references[path] = citation

    blocks = {block["id"]: block for block in document["blocks"]}
    evidence = {"company.fiscal_year": fiscal_evidence}
    errors = []
    for path in required:
        citation = references.get(path)
        if citation is None:
            errors.append(f"{path}: missing citation")
            continue
        try:
            hydrated = _hydrate_citation(path, citation, blocks)
        except ValueError as error:
            errors.append(str(error))
            continue
        section, field = path.split(".", 1)
        block = blocks[hydrated["block_id"]]
        if not _row_supports_value(
            path, data[section][field], block, hydrated["quote"]
        ):
            errors.append(
                f"{path}: cited row does not contain the reported quantity "
                "at its stated scale and sign"
            )
            continue
        evidence[path] = hydrated
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "data": data,
        "evidence": evidence,
        "validation": {
            "numeric_fields_with_citations": len(required),
            "numeric_fields_with_evidence": len(evidence),
            "deterministic_metadata_fields": ["company.fiscal_year"],
            "model_provided_fiscal_year_evidence": model_provided_fiscal_year_evidence,
            "canonicalized_bare_evidence_paths": canonicalized_bare_paths,
            "source_sha256": document["source_sha256"],
            "evidence_contract": "stable_source_row_v1",
            "quotes_hydrated_from_source": True,
            "semantic_accuracy_requires_separate_check": True,
        },
    }
