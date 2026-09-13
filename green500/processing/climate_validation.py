"""Reject fixed climate values whose source quotation contradicts their field."""

import re

_EXCLUDED_TARGET_TERMS = (
    "goal achieved",
    "target achieved",
    "expired",
    "withdrawn",
    "superseded",
    "cancelled",
    "canceled",
)
_LOCAL_TARGET_PATTERNS = (
    r"\b(?:facility|site|country|region|segment|business unit)[ -](?:specific|level|only)\b",
    r"\b(?:facility|site|country|region|segment|business unit)s?\s+target\b",
    r"\bselected\s+(?:facilities|sites|countries|regions|segments|business units|locations)\b",
)
_SOURCE_HINT_TERMS = (
    "assur",
    "base year",
    "baseline year",
    "calendar year",
    "carbon neutral",
    "carbon removal",
    "emissions by scope",
    "fiscal year",
    "goal achieved",
    "greenhouse gas",
    "intensity",
    "location-based",
    "market-based",
    "net zero",
    "net-zero",
    "renewable electricity",
    "scope 1",
    "scope 2",
    "scope 3",
    "supplier",
    "target",
)


def _text(value):
    return re.sub(r"\s+", " ", str(value or "")).casefold()


def climate_source_hints(evidence, max_count=32, max_chars=20_000):
    """Return exact high-signal source lines without supplying expected values."""
    candidates = []
    for block in evidence.get("blocks", []):
        lines = [line.strip() for line in block.get("text", "").splitlines()]
        for index, line in enumerate(lines):
            text = _text(line)
            terms = sum(term in text for term in _SOURCE_HINT_TERMS)
            if terms == 0:
                continue
            start = max(0, index - 1)
            end = min(len(lines), index + 2)
            excerpt = "\n".join(item for item in lines[start:end] if item)[:2000]
            if not re.search(r"\d", excerpt):
                continue
            candidates.append(
                {
                    "score": terms * 10 + min(len(re.findall(r"\d", excerpt)), 20),
                    "block_id": block["block_id"],
                    "excerpt": excerpt,
                }
            )
    ranked = sorted(
        candidates, key=lambda item: (-item["score"], item["block_id"], item["excerpt"])
    )
    prioritized = []
    for term in _SOURCE_HINT_TERMS:
        match = next(
            (candidate for candidate in ranked if term in _text(candidate["excerpt"])),
            None,
        )
        if match is not None:
            prioritized.append(match)
    prioritized.extend(ranked)
    selected = []
    used = 0
    seen = set()
    for candidate in prioritized:
        identity = (candidate["block_id"], candidate["excerpt"])
        if identity in seen or used + len(candidate["excerpt"]) > max_chars:
            continue
        seen.add(identity)
        selected.append(
            {
                "block_id": candidate["block_id"],
                "source_excerpt": candidate["excerpt"],
            }
        )
        used += len(candidate["excerpt"])
        if len(selected) == max_count:
            break
    return selected


def _has_scope_1(value):
    return re.search(r"\bscope\s*1\b", value) is not None


def _has_scope_2(value):
    return re.search(r"\bscope\s*2\b", value) is not None


def _has_scope_3(value):
    return re.search(r"\bscope\s*3\b", value) is not None


def _has_combined_scope_1_2(value):
    return re.search(
        r"\bscope\s*1\s*(?:&|\+|and)\s*(?:scope\s*)?2\b", value
    ) is not None


def _target_prefix(field):
    for attribute in (
        "baseline_year",
        "target_year",
        "reduction_pct",
        "progress_pct",
        "sbti_validated",
    ):
        suffix = "_" + attribute
        if field.startswith("climate_target_") and field.endswith(suffix):
            return field[: -len(suffix)], attribute
    return None, None


def _family_anchor(prefix, result, source_text, field, model_quote):
    """Use the target family's strongest semantic citation for shared attributes."""
    metadata = result.get("metadata", {})
    candidates = [_text(model_quote)]
    for attribute in ("reduction_pct", "progress_pct", "target_year"):
        details = metadata.get(prefix + "_" + attribute) or {}
        evidence = details.get("evidence") or {}
        quote = _text(evidence.get("quote"))
        if quote:
            candidates.append(quote)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            _validate_target_scope_and_type(field, candidate)
            _validate_named_target(field, candidate)
            return candidate
        except ValueError:
            continue
    return source_text


def _normalize_explicit_decrease(field, value, proof, source_text):
    """Represent a quoted reduction magnitude with the fixed negative-change sign."""
    if not (
        field.endswith("_change_pct")
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value < 0
        and any(
            term in source_text
            for term in ("reduc", "decreas", "fell", "lower", "↓")
        )
    ):
        return
    raw = str(proof.get("raw_value") or "").strip()
    magnitude = raw.removeprefix("-").replace(",", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", magnitude) is None:
        return
    candidates = re.findall(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?", source_text)
    matches = [token for token in candidates if token.replace(",", "") == magnitude]
    if len(matches) == 1 and abs(float(magnitude) + float(value)) < 0.000001:
        proof["raw_value"] = matches[0]
        proof["scale_factor"] = -1.0


def _validate_target_time_and_boundary(prefix, result, source_text):
    reporting_year = result.get("reporting_year")
    target_year = result["values"].get(prefix + "_target_year")
    if (
        target_year is not None
        and reporting_year is not None
        and target_year <= reporting_year
    ):
        raise ValueError("A selected target year must be after reporting_year.")
    if any(term in source_text for term in _EXCLUDED_TARGET_TERMS):
        raise ValueError("An achieved, expired or retired target cannot be selected.")
    if any(re.search(pattern, source_text) for pattern in _LOCAL_TARGET_PATTERNS) and not any(
        term in source_text
        for term in ("global", "company-wide", "companywide", "entire business")
    ):
        raise ValueError("A local or business-subset target cannot be selected.")


def invalid_climate_target_families(result):
    """Freeze family-level exclusions before field validation starts mutating values."""
    invalid = {}
    prefixes = {
        prefix
        for field in result.get("values", {})
        if (prefix := _target_prefix(field)[0]) is not None
    }
    for prefix in prefixes:
        target_year = result["values"].get(prefix + "_target_year")
        reporting_year = result.get("reporting_year")
        if (
            target_year is not None
            and reporting_year is not None
            and target_year <= reporting_year
        ):
            invalid[prefix] = "A selected target year must be after reporting_year."
            continue
        quotes = " ".join(
            _text(((result["metadata"].get(field) or {}).get("evidence") or {}).get("quote"))
            for field in result["values"]
            if field.startswith(prefix + "_")
        )
        if any(term in quotes for term in _EXCLUDED_TARGET_TERMS):
            invalid[prefix] = "An achieved, expired or retired target cannot be selected."
    return invalid


def _validate_target_scope_and_type(field, source_text):
    if (
        "_scope_" in field
        and _has_combined_scope_1_2(source_text)
        and _has_scope_3(source_text)
    ):
        raise ValueError(
            "A quotation containing multiple target scopes cannot establish one target slot."
        )
    if "_scope_1_2_3_" in field:
        if not (
            _has_combined_scope_1_2(source_text) and _has_scope_3(source_text)
        ):
            raise ValueError("The target quotation does not establish Scope 1, 2 and 3.")
    elif "_scope_1_2_" in field:
        if not _has_combined_scope_1_2(source_text) or _has_scope_3(source_text):
            raise ValueError(
                "The target quotation does not uniquely establish combined Scope 1 and 2."
            )
    elif "_scope_1_" in field:
        if (
            not _has_scope_1(source_text)
            or _has_scope_2(source_text)
            or _has_combined_scope_1_2(source_text)
        ):
            raise ValueError(
                "A combined Scope 1 and 2 target cannot populate Scope 1 alone."
            )
    elif "_scope_2_" in field:
        if (
            not _has_scope_2(source_text)
            or _has_scope_1(source_text)
            or _has_combined_scope_1_2(source_text)
        ):
            raise ValueError(
                "A combined Scope 1 and 2 target cannot populate Scope 2 alone."
            )
    elif "_scope_3_" in field and not _has_scope_3(source_text):
        raise ValueError("The target quotation does not establish Scope 3.")
    elif "_scope_3_" in field and re.search(
        r"\b(?:categor(?:y|ies)|selected|partial|subset)\b", source_text
    ):
        raise ValueError("Selected Scope 3 categories are not a complete Scope 3 target.")

    has_intensity = "intensity" in source_text or "per unit" in source_text
    has_absolute = "absolute" in source_text or (
        not has_intensity
        and "reduc" in source_text
        and any(term in source_text for term in ("emission", "ghg", "co2e"))
    )
    if "_absolute_reduction_" in field and (not has_absolute or has_intensity):
        raise ValueError(
            "An intensity or mixed target cannot populate an absolute target slot."
        )
    if "_intensity_reduction_" in field and (not has_intensity or has_absolute):
        raise ValueError(
            "An absolute or mixed target cannot populate an intensity target slot."
        )


def _validate_named_target(field, source_text):
    if "_net_zero_" in field:
        if re.search(r"\bnet[ -]?zero\b", source_text) is None:
            raise ValueError("Carbon neutrality cannot substitute for net zero.")
    elif "_carbon_neutrality_" in field:
        if re.search(r"\bcarbon neutral(?:ity)?\b", source_text) is None:
            raise ValueError("The quotation does not establish carbon neutrality.")
    elif "_renewable_electricity_" in field:
        if not ("renewable" in source_text and "electricity" in source_text):
            raise ValueError(
                "Carbon-free electricity cannot substitute for renewable electricity."
            )
    elif "_supplier_engagement_" in field and "supplier" not in source_text:
        raise ValueError("The quotation does not establish a supplier target.")


def _validate_current_field(field, source_text):
    if field == "climate_scope_1_tco2e" and (
        not _has_scope_1(source_text)
        or _has_scope_2(source_text)
        or _has_combined_scope_1_2(source_text)
    ):
        raise ValueError("The quotation does not establish Scope 1 alone.")
    if field == "climate_scope_2_market_tco2e" and not (
        _has_scope_2(source_text) and "market" in source_text
    ):
        raise ValueError("The quotation does not establish market-based Scope 2.")
    if field == "climate_scope_2_location_tco2e" and not (
        _has_scope_2(source_text) and "location" in source_text
    ):
        raise ValueError("The quotation does not establish location-based Scope 2.")
    if field == "climate_scope_3_tco2e" and not _has_scope_3(source_text):
        raise ValueError("The quotation does not establish Scope 3.")
    if field == "climate_scope_1_2_change_pct" and not _has_combined_scope_1_2(
        source_text
    ):
        raise ValueError("The quotation does not establish combined Scope 1 and 2 change.")
    if field == "climate_scope_3_change_pct" and (
        not _has_scope_3(source_text)
        or "intensity" in source_text
        or "per unit" in source_text
    ):
        raise ValueError("A Scope 3 intensity change is not an absolute Scope 3 change.")
    if field == "climate_ghg_intensity_change_pct" and not (
        ("intensity" in source_text or "per unit" in source_text)
        and (
            "emission" in source_text
            or "ghg" in source_text
            or "co2e" in source_text
        )
    ):
        raise ValueError("The quotation does not establish a GHG intensity change.")
    if field == "climate_renewable_electricity_pct" and not (
        "renewable" in source_text and "electricity" in source_text
    ):
        raise ValueError("The quotation does not establish renewable electricity.")
    if field == "climate_net_zero_target_year" and re.search(
        r"\bnet[ -]?zero\b", source_text
    ) is None:
        raise ValueError("Carbon neutrality cannot substitute for net zero.")
    if field == "climate_sbti_validated" and not (
        (
            "sbti" in source_text
            or "science based targets initiative" in source_text
            or "science-based targets initiative" in source_text
        )
        and re.search(r"\b(?:validated|approved)\b", source_text)
    ):
        raise ValueError("The quotation does not establish SBTi validation.")
    if field == "climate_ghg_inventory_assured" and not (
        any(term in source_text for term in ("assur", "verified", "reviewed"))
        and any(term in source_text for term in ("emission", "ghg", "inventory"))
    ):
        raise ValueError("The quotation does not establish GHG inventory assurance.")


def validate_climate_field(
    field,
    value,
    metadata,
    proof,
    source_quote,
    result,
    model_quote=None,
    source_context=None,
    invalid_target_families=None,
):
    """Reject source contradictions before accepting one climate field."""
    source_text = _text(source_quote)
    semantic_text = _text(source_context) or _text(model_quote) or source_text
    _normalize_explicit_decrease(field, value, proof, source_text)
    prefix, _attribute = _target_prefix(field)
    if prefix is not None:
        if prefix in (invalid_target_families or {}):
            raise ValueError(invalid_target_families[prefix])
        anchor = _family_anchor(prefix, result, source_text, field, semantic_text)
        _validate_target_time_and_boundary(prefix, result, anchor)
        _validate_target_scope_and_type(field, anchor)
        _validate_named_target(field, anchor)
        if _attribute == "baseline_year" and not re.search(
            r"\b(?:baseline|base year|from\s+20\d{2})\b", semantic_text
        ):
            raise ValueError("The quotation does not explicitly establish a baseline year.")
        if _attribute == "reduction_pct" and "reduc" not in semantic_text:
            raise ValueError("The quotation does not establish a required reduction percentage.")
        if _attribute == "progress_pct" and not re.search(
            r"\b(?:progress|achieved|to date|reduced by)\b", semantic_text
        ):
            raise ValueError("The quotation does not directly report target progress.")
    else:
        _validate_current_field(field, semantic_text)
        if field == "climate_net_zero_target_year":
            _validate_target_time_and_boundary("climate_net_zero", result, source_text)
