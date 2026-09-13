"""Parse public S&P Sustainable1 score pages without inferring missing identity or dates."""

import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

SCORE_URL = "https://www.spglobal.com/sustainable1/en/scores/results?cid={provider_cid}"
SCORE_HEADERS = (
    "Company",
    "Industry",
    "CSA Score",
    "ESG Score",
    "Score Under Review",
    "Last Updated",
)
LEGAL_SUFFIXES = (
    ("public", "limited", "company"),
    ("limited", "partnership"),
    ("incorporated",),
    ("corporation",),
    ("company",),
    ("limited",),
    ("inc",),
    ("corp",),
    ("co",),
    ("plc",),
    ("ltd",),
    ("llc",),
    ("llp",),
    ("lp",),
    ("n", "v"),
    ("s", "a"),
    ("a", "g"),
    ("p", "l", "c"),
    ("l", "p"),
    ("s", "e"),
)


class _VisibleTextParser(HTMLParser):
    """Collect visible text from a provider HTML response without executing content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1
        elif tag in {"br", "p", "div", "section", "tr", "td", "th", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "div", "section", "tr", "td", "th", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def score_url(provider_cid: str) -> str:
    """Return the canonical public result URL for an explicitly verified provider ID."""
    if not isinstance(provider_cid, str) or not re.fullmatch(
        r"[1-9][0-9]*", provider_cid
    ):
        raise ValueError("S&P provider CID must be a positive decimal integer.")
    if int(provider_cid) > 9_223_372_036_854_775_807:
        raise ValueError("S&P provider CID exceeds the supported integer range.")
    return SCORE_URL.format(provider_cid=provider_cid)


def validate_captured_score_url(captured_url: str, expected_provider_cid: str) -> str:
    """Reject a browser page left on a different company after failed navigation."""
    score_url(expected_provider_cid)
    parsed = urlsplit(captured_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.spglobal.com"
        or parsed.path.rstrip("/")
        not in {
            "/sustainable1/en/scores/results",
            "/content/spglobal/sustainable1/us/en/scores/results.html",
        }
    ):
        raise ValueError("Captured S&P score URL is not the public result page.")
    cids = parse_qs(parsed.query).get("cid", [])
    if cids != [expected_provider_cid]:
        raise ValueError("Captured S&P score URL does not match the requested CID.")
    return captured_url


def normalize_issuer_name(name: str) -> str:
    """Normalize issuer form while preserving words that identify a business boundary."""
    if not isinstance(name, str):
        raise TypeError("S&P issuer name must be text.")
    text = re.sub(
        r"\s*\(\s*class\s+[a-z0-9]+\s*\)\s*$",
        "",
        unicodedata.normalize("NFKC", name),
        flags=re.IGNORECASE,
    )
    tokens = re.sub(
        r"[^\w]+", " ", text.casefold().replace("&", " and "), flags=re.UNICODE
    ).split()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    removed_suffix = True
    while removed_suffix:
        removed_suffix = False
        for suffix in LEGAL_SUFFIXES:
            if len(tokens) > len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                tokens = tokens[: -len(suffix)]
                removed_suffix = True
                break
    return " ".join(tokens)


def match_official_candidates(company: dict, candidates: list[dict]) -> dict:
    """Accept exactly one CID whose official displayed name matches a declared issuer name."""
    expected_names = {
        normalize_issuer_name(name)
        for name in (company.get("normalized_issuer_names") or [])
    }
    if not expected_names:
        raise ValueError("S&P company identity has no normalized issuer names.")
    matches = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise TypeError("S&P search candidate must be an object.")
        provider_cid = str(candidate.get("provider_cid", ""))
        provider_name = candidate.get("provider_company_name")
        try:
            score_url(provider_cid)
        except ValueError:
            continue
        if (
            isinstance(provider_name, str)
            and normalize_issuer_name(provider_name) in expected_names
        ):
            matches[provider_cid] = provider_name.strip()
    if not matches:
        return {"status": "unmatched", "match_method": None}
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "match_method": "exact_normalized_issuer_name",
            "candidate_cids": sorted(matches),
        }
    provider_cid, provider_name = next(iter(matches.items()))
    return {
        "status": "matched",
        "match_method": "exact_normalized_issuer_name",
        "provider_cid": provider_cid,
        "provider_company_name": provider_name,
    }


def _response_text(body: bytes, content_type: str) -> str:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("S&P score response is not UTF-8 text.") from error
    if content_type.startswith("application/json") or text.lstrip().startswith('"'):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("S&P score reader response is malformed JSON.") from error
        if not isinstance(decoded, str):
            raise TypeError("S&P score reader response must contain text.")
        return decoded
    if "html" in content_type or re.search(r"<html\b", text, re.IGNORECASE):
        parser = _VisibleTextParser()
        parser.feed(text)
        return "".join(parser.parts)
    return text


def _normalized_lines(text: str) -> list[str]:
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"^L[0-9]+:\s*", "", raw_line.strip())
        line = re.sub(r"\s*cite.*$", "", line).strip()
        if line:
            lines.append(" ".join(line.split()))
    return lines


def _score_blocks(lines: list[str]) -> list[tuple[str, str, int, int, bool, date]]:
    blocks = []
    header_count = len(SCORE_HEADERS)
    for index in range(len(lines) - header_count):
        if tuple(lines[index : index + header_count]) != SCORE_HEADERS:
            continue
        values = lines[index + header_count : index + header_count + header_count]
        if len(values) != header_count:
            continue
        try:
            csa_score = int(values[2])
            esg_score = int(values[3])
            updated = (
                datetime.strptime(values[5], "%B %d, %Y").replace(tzinfo=UTC).date()
            )
        except ValueError:
            continue
        under_review = values[4].upper()
        if not 0 <= csa_score <= 100 or not 0 <= esg_score <= 100:
            raise ValueError("S&P score values must be between 0 and 100.")
        if under_review not in {"YES", "NO"}:
            raise ValueError("S&P score review status must be YES or NO.")
        blocks.append(
            (values[0], values[1], csa_score, esg_score, under_review == "YES", updated)
        )
    return blocks


def parse_public_score_response(
    body: bytes,
    *,
    provider_cid: str,
    captured_url: str,
    fetched_at: datetime,
    content_type: str = "text/html",
) -> dict:
    """Parse the public headline scores while leaving an unreported assessment year null."""
    provider_url = validate_captured_score_url(captured_url, provider_cid)
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("S&P score fetch time must include a time-zone offset.")
    text = _response_text(body, content_type)
    lines = _normalized_lines(text)
    blocks = _score_blocks(lines)
    if len(set(blocks)) > 1:
        raise ValueError(
            "S&P score response contains conflicting headline score blocks."
        )
    participation_notices = [
        line.casefold() for line in lines if line.casefold().startswith("this company")
    ]
    if any(
        line.startswith(
            "this company is a corporate sustainability assessment (csa) survey respondent."
        )
        for line in participation_notices
    ):
        is_csa_survey_respondent = True
    elif any(
        line.startswith(
            "this company's esg score is based on publicly available information"
        )
        and "not based on the company's active participation" in line
        for line in participation_notices
    ):
        is_csa_survey_respondent = False
    else:
        is_csa_survey_respondent = None
    joined_text = " ".join(lines).casefold()
    common = {
        "provider": "S&P Global Sustainable1",
        "provider_cid": provider_cid,
        "provider_url": provider_url,
        "canonical_provider_url": score_url(provider_cid),
        "assessment_year": None,
        "is_csa_survey_respondent": is_csa_survey_respondent,
        "available_dimension_scores": [],
        "source_sha256": hashlib.sha256(body).hexdigest(),
        "fetched_at": fetched_at.isoformat(),
    }
    if not blocks:
        premium_message = "available via our premium channels"
        if premium_message not in joined_text:
            raise ValueError(
                "S&P score response contains no complete headline score block."
            )
        header_count = len(SCORE_HEADERS)
        for index in range(len(lines) - header_count):
            if tuple(lines[index : index + header_count]) == SCORE_HEADERS:
                values = lines[index + header_count :]
                if len(values) >= 2:
                    return common | {
                        "public_score_status": "premium_only",
                        "company_name": values[0],
                        "industry": values[1],
                        "csa_score": None,
                        "esg_score": None,
                        "scores": [],
                        "score_under_review": None,
                        "last_updated": None,
                    }
        raise ValueError("S&P premium-only response has no company identity block.")
    company, industry, csa_score, esg_score, under_review, updated = blocks[0]
    return {
        **common,
        "public_score_status": "available",
        "company_name": company,
        "industry": industry,
        "csa_score": csa_score,
        "esg_score": esg_score,
        "scores": [
            {"score_type": "csa_score", "value": csa_score},
            {"score_type": "esg_score", "value": esg_score},
        ],
        "score_under_review": under_review,
        "last_updated": updated.isoformat(),
    }


def build_company_coverage(
    companies: list[dict], verified_cids: dict[str, dict]
) -> list[dict]:
    """Describe provider-ID coverage for every company without guessing a fuzzy match."""
    rows = []
    seen_ciks = set()
    seen_provider_cids = set()
    for company in companies:
        cik = company["cik"]
        if cik in seen_ciks:
            raise ValueError("S&P score coverage contains a duplicate company CIK.")
        seen_ciks.add(cik)
        mapping = verified_cids.get(cik)
        if mapping:
            provider_cid = mapping["provider_cid"]
            score_url(provider_cid)
            validate_captured_score_url(mapping["mapping_evidence_url"], provider_cid)
            provider_company_name = mapping.get("company_name")
            if (
                not isinstance(provider_company_name, str)
                or not provider_company_name.strip()
            ):
                raise ValueError(
                    "S&P score mapping has no reviewed provider company name."
                )
            if provider_cid in seen_provider_cids:
                raise ValueError(
                    "S&P score coverage reuses a provider CID across companies."
                )
            seen_provider_cids.add(provider_cid)
            rows.append(
                {
                    "company_cik": cik,
                    "company_name": company["name"],
                    "symbols": company["symbols"],
                    "outcome": "not_checked",
                    "identity_status": "matched",
                    "access_status": "not_requested",
                    "score_status": "not_checked",
                    "provider_cid": provider_cid,
                    "provider_company_name": provider_company_name.strip(),
                    "provider_url": score_url(provider_cid),
                    "mapping_evidence_url": mapping["mapping_evidence_url"],
                    "csa_score": None,
                    "esg_score": None,
                    "assessment_year": None,
                    "is_csa_survey_respondent": None,
                    "last_updated": None,
                    "source_sha256": None,
                    "fetched_at": None,
                    "reason": "Provider CID is verified; current score-page access has not run.",
                }
            )
        else:
            rows.append(
                {
                    "company_cik": cik,
                    "company_name": company["name"],
                    "symbols": company["symbols"],
                    "outcome": "not_checked",
                    "identity_status": "not_checked",
                    "access_status": "not_requested",
                    "score_status": "not_checked",
                    "provider_cid": None,
                    "provider_company_name": None,
                    "provider_url": None,
                    "mapping_evidence_url": None,
                    "csa_score": None,
                    "esg_score": None,
                    "assessment_year": None,
                    "is_csa_survey_respondent": None,
                    "last_updated": None,
                    "source_sha256": None,
                    "fetched_at": None,
                    "reason": "Public provider CID lookup has not yet completed for this company.",
                }
            )
    unused_ciks = set(verified_cids) - seen_ciks
    if unused_ciks:
        raise ValueError(
            "S&P score coverage contains mappings outside the company universe."
        )
    return rows


def apply_score_observations(
    coverage_rows: list[dict], observations: list[dict]
) -> list[dict]:
    """Publish verified captures only when CID and provider company name both match."""
    by_cid = {}
    for observation in observations:
        provider_cid = observation.get("provider_cid")
        if provider_cid in by_cid:
            raise ValueError("S&P score observations contain a duplicate provider CID.")
        by_cid[provider_cid] = observation
    result = []
    used_cids = set()
    for original in coverage_rows:
        row = dict(original)
        provider_cid = row.get("provider_cid")
        observation = by_cid.get(provider_cid)
        if observation is None:
            result.append(row)
            continue
        if row.get("identity_status") != "matched":
            raise ValueError("S&P score observation has no verified company mapping.")
        mapping_name = row.get("provider_company_name")
        if not mapping_name or observation.get("company_name") != mapping_name:
            raise ValueError(
                "S&P score observation company name does not match its mapping."
            )
        if observation.get("provider_url") != row.get("provider_url"):
            raise ValueError("S&P score observation URL does not match its mapping.")
        is_available = observation.get("public_score_status") == "available"
        row.update(
            outcome="matched" if is_available else "blocked",
            access_status="succeeded",
            score_status="available" if is_available else "premium_only",
            csa_score=observation["csa_score"],
            esg_score=observation["esg_score"],
            assessment_year=observation["assessment_year"],
            is_csa_survey_respondent=observation["is_csa_survey_respondent"],
            last_updated=observation["last_updated"],
            source_sha256=observation["source_sha256"],
            fetched_at=observation["fetched_at"],
            provider_company_name=observation["company_name"],
            provider_industry=observation["industry"],
            score_under_review=observation["score_under_review"],
            reason=None
            if is_available
            else "Public page directs score access to premium channels.",
        )
        result.append(row)
        used_cids.add(provider_cid)
    if set(by_cid) != used_cids:
        raise ValueError(
            "S&P score observation is outside the coverage company universe."
        )
    return result


def validate_approved_export(body: bytes, verified_cids: dict[str, dict]) -> dict:
    """Validate a licensed JSON export without publishing or changing provider values."""
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("S&P licensed export is not valid UTF-8 JSON.") from error
    required_document_fields = {
        "provider",
        "license_reference",
        "fetched_at",
        "rows",
    }
    if not isinstance(document, dict) or set(document) != required_document_fields:
        raise ValueError("S&P licensed export has unsupported document fields.")
    if document["provider"] != "S&P Global Sustainable1":
        raise ValueError("S&P licensed export has an unexpected provider.")
    license_reference = document["license_reference"]
    if not isinstance(license_reference, str) or not license_reference.strip():
        raise ValueError("S&P licensed export requires a usage-rights reference.")
    try:
        fetched_at = datetime.fromisoformat(document["fetched_at"])
    except (TypeError, ValueError) as error:
        raise ValueError("S&P licensed export fetch time must be ISO 8601.") from error
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError(
            "S&P licensed export fetch time must include a time-zone offset."
        )
    if not isinstance(document["rows"], list):
        raise TypeError("S&P licensed export rows must be a list.")
    allowed_score_types = {
        "csa_score",
        "esg_score",
        "environmental_score",
        "social_score",
        "governance_economic_score",
    }
    allowed_row_fields = {
        "company_cik",
        "provider_cid",
        "provider_company_name",
        "industry",
        "score_type",
        "score_value",
        "score_under_review",
        "last_updated",
        "assessment_year",
    }
    normalized_rows = []
    identities = set()
    for index, row in enumerate(document["rows"]):
        if not isinstance(row, dict) or set(row) != allowed_row_fields:
            raise ValueError(f"S&P licensed export row {index} has unsupported fields.")
        cik = row["company_cik"]
        if not isinstance(cik, str) or not re.fullmatch(r"[0-9]{10}", cik):
            raise ValueError(f"S&P licensed export row {index} has an invalid CIK.")
        provider_cid = row["provider_cid"]
        score_url(provider_cid)
        mapping = verified_cids.get(cik)
        if (
            not isinstance(mapping, dict)
            or mapping.get("provider_cid") != provider_cid
            or mapping.get("company_name") != row["provider_company_name"]
        ):
            raise ValueError(
                f"S&P licensed export row {index} does not match a verified company identity."
            )
        company_name = row["provider_company_name"]
        industry = row["industry"]
        if not isinstance(company_name, str) or not company_name.strip():
            raise ValueError(
                f"S&P licensed export row {index} has no provider company name."
            )
        if not isinstance(industry, str) or not industry.strip():
            raise ValueError(
                f"S&P licensed export row {index} has no provider industry."
            )
        score_type = row["score_type"]
        if score_type not in allowed_score_types:
            raise ValueError(
                f"S&P licensed export row {index} has an unsupported score type."
            )
        score_value = row["score_value"]
        if (
            isinstance(score_value, bool)
            or not isinstance(score_value, (int, float))
            or not 0 <= score_value <= 100
        ):
            raise ValueError(
                f"S&P licensed export row {index} has an invalid score value."
            )
        under_review = row["score_under_review"]
        if under_review is not None and not isinstance(under_review, bool):
            raise ValueError(
                f"S&P licensed export row {index} has an invalid review flag."
            )
        try:
            last_updated = date.fromisoformat(row["last_updated"])
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"S&P licensed export row {index} has an invalid last-updated date."
            ) from error
        assessment_year = row["assessment_year"]
        if assessment_year is not None and (
            isinstance(assessment_year, bool)
            or not isinstance(assessment_year, int)
            or not 1999 <= assessment_year <= 2100
        ):
            raise ValueError(
                f"S&P licensed export row {index} has an invalid assessment year."
            )
        identity = (cik, provider_cid, score_type, assessment_year, last_updated)
        if identity in identities:
            raise ValueError("S&P licensed export contains a duplicate score row.")
        identities.add(identity)
        normalized_rows.append(
            dict(
                row,
                provider_company_name=company_name.strip(),
                industry=industry.strip(),
                last_updated=last_updated.isoformat(),
                provider_url=score_url(provider_cid),
            )
        )
    return {
        "provider": document["provider"],
        "license_reference": license_reference.strip(),
        "fetched_at": fetched_at.isoformat(),
        "source_sha256": hashlib.sha256(body).hexdigest(),
        "rows": normalized_rows,
    }
