"""Load the official score snapshot into a small public catalog response."""

import json
import re
import threading
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCORE_FILE = (
    Path(__file__).parents[1] / "data" / "spglobal_scores" / "company_coverage.json"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CIK_PATTERN = re.compile(r"^[0-9]{10}$")
PUBLIC_FIELDS = (
    "esg_score",
    "csa_score",
    "status",
    "provider_cid",
    "last_updated",
    "fetched_at",
    "assessment_year",
    "provider_company_name",
    "provider_industry",
    "provider_url",
    "score_under_review",
    "is_csa_survey_respondent",
    "source_sha256",
)
_CACHE_LOCK = threading.Lock()
_CACHE = {"identity": None, "scores": {}, "updated_at": None}


def empty_score(status: str = "not_checked") -> dict:
    """Return the stable public shape without inventing missing provider values."""
    value = {field: None for field in PUBLIC_FIELDS}
    value["status"] = status
    return value


def _score_url_is_valid(url: object, provider_cid: object) -> bool:
    if not isinstance(url, str) or not isinstance(provider_cid, str):
        return False
    if not re.fullmatch(r"[1-9][0-9]*", provider_cid):
        return False
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "www.spglobal.com"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and parsed.path.rstrip("/") == "/sustainable1/en/scores/results"
        and not parsed.fragment
        and query == {"cid": [provider_cid]}
    )


def _number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Score must be numeric.")
    if not 0 <= value <= 100:
        raise ValueError("Score must be between 0 and 100.")
    return value


def _date(value: object) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Score date must be an ISO date.")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Score fetch time must be text.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Score fetch time must include a time-zone offset.")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError("Provider identity text is missing or too long.")
    return value.strip()


def _provenance(row: dict) -> dict:
    provider_cid = row.get("provider_cid")
    provider_url = row.get("provider_url")
    digest = row.get("source_sha256")
    if not _score_url_is_valid(provider_url, provider_cid):
        raise ValueError("Provider score URL does not match its CID.")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise ValueError("Score source SHA-256 is invalid.")
    assessment_year = row.get("assessment_year")
    if assessment_year is not None and (
        isinstance(assessment_year, bool)
        or not isinstance(assessment_year, int)
        or not 1999 <= assessment_year <= 2100
    ):
        raise ValueError("Assessment year is invalid.")
    return {
        "provider_cid": provider_cid,
        "fetched_at": _timestamp(row.get("fetched_at")),
        "assessment_year": assessment_year,
        "provider_company_name": _text(row.get("provider_company_name")),
        "provider_industry": _text(row.get("provider_industry")),
        "provider_url": provider_url,
        "source_sha256": digest,
    }


def describe_score(row: object) -> dict:
    """Validate one source row and return only fields approved for the viewer."""
    if not isinstance(row, dict):
        return empty_score("invalid")
    try:
        if (
            row.get("identity_status") == "ambiguous"
            or row.get("outcome") == "ambiguous"
        ):
            return empty_score("ambiguous")
        if (
            row.get("identity_status") == "unmatched"
            or row.get("outcome") == "unmatched"
        ):
            return empty_score("unmatched")
        if (
            row.get("access_status") == "blocked"
            or row.get("outcome") == "access_blocked"
        ):
            return empty_score("access_blocked")
        if row.get("outcome") == "not_checked":
            return empty_score("not_checked")
        provenance = _provenance(row)
        if (
            row.get("identity_status") != "matched"
            or row.get("access_status") != "succeeded"
        ):
            return empty_score("invalid")
        if (
            row.get("score_status") == "premium_only"
            and row.get("outcome") == "blocked"
        ):
            if row.get("csa_score") is not None or row.get("esg_score") is not None:
                return empty_score("invalid")
            participation = row.get("is_csa_survey_respondent")
            if participation is not None and not isinstance(participation, bool):
                return empty_score("invalid")
            return (
                empty_score("premium_only")
                | provenance
                | {"is_csa_survey_respondent": participation}
            )
        if row.get("score_status") != "available" or row.get("outcome") != "matched":
            return empty_score("invalid")
        under_review = row.get("score_under_review")
        if not isinstance(under_review, bool):
            return empty_score("invalid")
        participation = row.get("is_csa_survey_respondent")
        if participation is not None and not isinstance(participation, bool):
            return empty_score("invalid")
        return {
            "esg_score": _number(row.get("esg_score")),
            "csa_score": _number(row.get("csa_score")),
            "status": "available",
            "last_updated": _date(row.get("last_updated")),
            **provenance,
            "score_under_review": "YES" if under_review else "NO",
            "is_csa_survey_respondent": participation,
        }
    except (TypeError, ValueError):
        return empty_score("invalid")


def _read_scores(path: Path) -> tuple[dict[str, dict], str | None]:
    try:
        document = json.loads(path.read_bytes())
        rows = document["companies"]
        updated_at = document["generated_at"]
        _timestamp(updated_at)
        if not isinstance(rows, list):
            raise TypeError("Score company rows must be a list.")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}, None
    scores = {}
    duplicate_ciks = set()
    for row in rows:
        cik = row.get("company_cik") if isinstance(row, dict) else None
        if not isinstance(cik, str) or not CIK_PATTERN.fullmatch(cik):
            continue
        if cik in scores:
            duplicate_ciks.add(cik)
        scores[cik] = describe_score(row)
    for cik in duplicate_ciks:
        scores[cik] = empty_score("invalid")
    return scores, updated_at


def load_scores(path: Path = SCORE_FILE) -> tuple[dict[str, dict], str | None]:
    """Reload an atomically replaced snapshot by file identity and fail closed on errors."""
    try:
        stat = path.stat()
        identity = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}, None
    with _CACHE_LOCK:
        if _CACHE["identity"] != identity:
            scores, updated_at = _read_scores(path)
            _CACHE.update(identity=identity, scores=scores, updated_at=updated_at)
        return dict(_CACHE["scores"]), _CACHE["updated_at"]
