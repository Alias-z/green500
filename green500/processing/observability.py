"""Record direct Green500 model calls in the shared LLM event schema."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AI_FUNCTION = "green500"
PROJECT = "green500"
OPERATION = "financial_extraction_benchmark"
_DEFAULT_EVENT_DIRECTORY = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "runtime"
    / "observability"
    / "events"
)
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_COST_BASIS = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_WRITE_LIMIT_BYTES = 32_000
_LOGGER = logging.getLogger(__name__)


def _nonnegative_integer(
    value: Any, field: str, *, required: bool = False
) -> int | None:
    """Return an exact provider counter without accepting booleans or coercion."""
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _provider_timestamp(value: Any) -> str:
    """Return the provider's required UTC response timestamp."""
    created = _nonnegative_integer(value, "provider_response.created", required=True)
    try:
        return datetime.fromtimestamp(created, UTC).isoformat()
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError(
            "provider_response.created is outside the supported range"
        ) from error


def _usage_fields(raw_usage: Any) -> dict[str, Any]:
    """Normalize OpenAI-compatible usage into the versioned shared event fields."""
    if not isinstance(raw_usage, Mapping):
        raise TypeError("provider_response.usage must be an object")

    prompt = _nonnegative_integer(
        raw_usage.get("prompt_tokens"), "prompt_tokens", required=True
    )
    completion = _nonnegative_integer(
        raw_usage.get("completion_tokens"), "completion_tokens", required=True
    )
    total = _nonnegative_integer(
        raw_usage.get("total_tokens"), "total_tokens", required=True
    )
    assert prompt is not None and completion is not None and total is not None

    fields: dict[str, Any] = {
        "usage_schema_version": 2,
        "usage_prompt_tokens": prompt,
        "usage_completion_tokens": completion,
        "usage_total_tokens": total,
        "prompt_tokens_status": "reported",
        "completion_tokens_status": "reported",
        "total_tokens_status": (
            "reported_consistent"
            if total == prompt + completion
            else "reported_inconsistent"
        ),
        "cost_model": "token",
        "pricing_source": "provider_receipt_direct_http",
        "request_count": 1,
    }

    prompt_details = raw_usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, Mapping) else {}
    cached = _nonnegative_integer(prompt_details.get("cached_tokens"), "cached_tokens")
    if cached is None:
        fields.update(
            cache_read_status="unreported",
            cache_creation_status="unreported",
            cache_breakdown_status="unreported",
        )
    else:
        fields.update(
            usage_cached_tokens=cached,
            cache_read_status="reported",
            cache_creation_status="unreported",
            cache_breakdown_status=(
                "reported_valid" if cached <= prompt else "reported_inconsistent"
            ),
        )

    input_text = _nonnegative_integer(
        prompt_details.get("text_tokens"), "input_text_tokens"
    )
    if input_text is not None:
        fields["usage_input_text_tokens"] = input_text
        fields["input_text_tokens_status"] = (
            "reported" if input_text <= prompt else "reported_inconsistent"
        )

    completion_details = raw_usage.get("completion_tokens_details")
    completion_details = (
        completion_details if isinstance(completion_details, Mapping) else {}
    )
    reasoning = _nonnegative_integer(
        completion_details.get("reasoning_tokens"), "reasoning_tokens"
    )
    answer = _nonnegative_integer(
        completion_details.get("text_tokens"), "answer_tokens"
    )
    if reasoning is not None:
        fields["usage_reasoning_tokens"] = reasoning
        fields["reasoning_tokens_status"] = (
            "reported" if reasoning <= completion else "reported_inconsistent"
        )
    if answer is not None:
        fields["usage_answer_tokens"] = answer
        fields["answer_tokens_status"] = (
            "reported"
            if answer <= completion and (reasoning or 0) + answer <= completion
            else "reported_inconsistent"
        )
    elif reasoning is not None and reasoning <= completion:
        fields["usage_answer_tokens"] = completion - reasoning
        fields["answer_tokens_status"] = "derived_from_reported_reasoning"
    return fields


def _event_identity(receipt_digest: str, observation_event: str) -> str:
    """Return a stable identity so replaying one receipt cannot append it twice."""
    return hashlib.sha256(
        f"{PROJECT}\0{OPERATION}\0{receipt_digest}\0{observation_event}".encode()
    ).hexdigest()[:32]


def _existing_event_identities(paths: set[Path]) -> set[str]:
    """Read Green500 identities from the two dates relevant to one receipt."""
    identities: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if '"green500_event_id"' not in line:
                        continue
                    try:
                        payload = json.loads(line).get("payload", {})
                    except (AttributeError, json.JSONDecodeError):
                        continue
                    event_id = str(payload.get("green500_event_id") or "")
                    if event_id:
                        identities.add(event_id)
        except OSError:
            continue
    return identities


def _write_receipt_marker(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically remember a pending or completed receipt projection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_event(path: Path, event: Mapping[str, Any]) -> None:
    """Append one bounded JSON line with one operating-system write."""
    line = (
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    if len(line) > _WRITE_LIMIT_BYTES:
        raise ValueError("Green500 LLM observation exceeds the bounded event size")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        written = os.write(descriptor, line)
        if written != len(line):
            raise OSError("Green500 LLM observation append was incomplete")
    finally:
        os.close(descriptor)


def record_successful_http_benchmark_receipt(
    receipt_path: str | Path,
    *,
    provider: str,
    plan: str,
    company_cik: str,
    trial: str,
    input_version: str,
    output_token_budget: int | str = "provider_default",
    cost_cny: float | None = None,
    cost_basis: str | None = None,
    pricing_source: str = "green500_verified_cost_input",
    event_directory: str | Path | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Append usage and latency events for one completed direct HTTP call.

    The raw receipt remains the authority for provider response content. This
    projection records only route, usage, timing and bounded benchmark metadata.
    Calling it again for the same receipt is a no-op. When known, a verified
    cost must be supplied on this first append because event rows are immutable.
    """
    path = Path(receipt_path)
    raw_bytes = path.read_bytes()
    receipt_digest = hashlib.sha256(raw_bytes).hexdigest()
    receipt = json.loads(raw_bytes)
    if not isinstance(receipt, Mapping):
        raise TypeError("benchmark receipt must be a JSON object")
    status_code = receipt.get("status_code")
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 200 <= status_code < 300
    ):
        raise ValueError("only successful provider receipts can record usage")
    response = receipt.get("provider_response")
    if not isinstance(response, Mapping):
        raise TypeError("successful benchmark receipt has no provider_response object")

    provider_name = str(provider or "").strip().lower()
    plan_name = str(plan or "").strip().lower()
    model = str(response.get("model") or receipt.get("model_requested") or "").strip()
    request_id = str(response.get("id") or "").strip()
    if not provider_name or plan_name not in {"agent", "api", "coding"} or not model:
        raise ValueError("provider, supported plan and model are required")
    if not request_id or len(request_id) > 256:
        raise ValueError("provider_response.id must be a bounded non-empty string")
    for label, value in {
        "company_cik": company_cik,
        "trial": trial,
        "input_version": input_version,
    }.items():
        if not str(value or "").strip() or len(str(value)) > 160:
            raise ValueError(f"{label} must be a bounded non-empty string")

    request_digest = str(receipt.get("request_sha256") or "").strip().lower()
    if not _HEX_64.fullmatch(request_digest):
        raise ValueError("receipt request_sha256 must be a lowercase SHA-256")
    thinking = receipt.get("thinking_requested")
    if not isinstance(thinking, bool):
        raise TypeError("receipt thinking_requested must be a boolean")
    effort = "default" if thinking else "disabled"
    elapsed_seconds = receipt.get("elapsed_seconds")
    if (
        isinstance(elapsed_seconds, bool)
        or not isinstance(elapsed_seconds, (int, float))
        or elapsed_seconds < 0
    ):
        raise ValueError("receipt elapsed_seconds must be a nonnegative number")
    elapsed_ms = round(float(elapsed_seconds) * 1000)
    usage = _usage_fields(response.get("usage"))
    cost_fields: dict[str, Any] = {}
    if cost_cny is not None:
        if (
            isinstance(cost_cny, bool)
            or not isinstance(cost_cny, (int, float))
            or not math.isfinite(float(cost_cny))
            or float(cost_cny) < 0
        ):
            raise ValueError("cost_cny must be a finite nonnegative number")
        normalized_basis = str(cost_basis or "").strip().lower()
        normalized_pricing_source = str(pricing_source or "").strip().lower()
        if not _COST_BASIS.fullmatch(normalized_basis):
            raise ValueError("cost_basis must be a bounded lowercase metric label")
        if not _COST_BASIS.fullmatch(normalized_pricing_source):
            raise ValueError("pricing_source must be a bounded lowercase metric label")
        cost_fields = {
            "cost_cny": float(cost_cny),
            "cost_currency": "CNY",
            "cost_basis": normalized_basis,
            "pricing_source": normalized_pricing_source,
            "token_cost_evidence_status": "complete",
        }
    provider_time = _provider_timestamp(response.get("created"))
    recording_time = (recorded_at or datetime.now(UTC)).astimezone(UTC)
    recorded_timestamp = recording_time.isoformat()

    call_id = hashlib.sha256(
        f"{PROJECT}\0{request_digest}\0{request_id}".encode()
    ).hexdigest()[:16]
    usage_event_id = hashlib.sha256(
        f"{PROJECT}\0usage\0{receipt_digest}\0{request_id}".encode()
    ).hexdigest()[:32]
    common = {
        "event_kind": "llm_call",
        "project": PROJECT,
        "operation": OPERATION,
        "caller_operation": OPERATION,
        "ai_function": AI_FUNCTION,
        "spider": PROJECT,
        "provider": provider_name,
        "plan": plan_name,
        "model": model,
        "model_snapshot": model,
        "effort": effort,
        "task": "responses",
        "image_detail": "none",
        "output_token_budget": output_token_budget,
        "key_label": f"{provider_name}_{plan_name}",
        "caller_call_id": call_id,
        "call_id": call_id,
        "provider_request_id": request_id,
        "provider_timestamp": provider_time,
        "recorded_at": recorded_timestamp,
        "delayed_observation": True,
        "company_cik": str(company_cik),
        "trial": str(trial),
        "input_version": str(input_version),
        "request_sha256": request_digest,
        "receipt_sha256": receipt_digest,
        "attempt": 1,
        "max_attempts": 1,
    }
    provider_event_id = _event_identity(receipt_digest, "provider_response")
    success_event_id = _event_identity(receipt_digest, "attempt_success")
    events = [
        {
            "ts": provider_time,
            "kind": "llm",
            "payload": {
                **common,
                **usage,
                **cost_fields,
                "green500_event_id": provider_event_id,
                "observation_event": "provider_response",
                "call_state": "succeeded",
                "elapsed_ms": elapsed_ms,
                "usage_event_id": usage_event_id,
                "usage_record_version": 2,
            },
        },
        {
            "ts": provider_time,
            "kind": "llm",
            "payload": {
                **common,
                "green500_event_id": success_event_id,
                "observation_event": "attempt_success",
                "call_state": "succeeded",
                "elapsed_ms": elapsed_ms,
            },
        },
    ]

    root = Path(
        event_directory
        or os.getenv("GREEN500_OBSERVABILITY_EVENT_DIRECTORY")
        or _DEFAULT_EVENT_DIRECTORY
    )
    root.mkdir(parents=True, exist_ok=True)
    provider_day = provider_time[:10]
    recording_day = recording_time.date().isoformat()
    destination = root / f"{provider_day}.jsonl"
    marker = root / ".green500_receipts" / f"{receipt_digest}.json"
    lock_path = root / ".green500-observability.lock"
    appended = []
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if marker.exists():
            saved = json.loads(marker.read_text(encoding="utf-8"))
            if saved.get("projection_status", "complete") == "complete":
                known = {provider_event_id, success_event_id}
            else:
                known = _existing_event_identities(
                    {destination, root / f"{recording_day}.jsonl"}
                )
        else:
            # A new receipt has no earlier append. Only an interrupted pending
            # marker needs a ledger scan; ordinary calls stay constant-time.
            _write_receipt_marker(
                marker,
                {"receipt_sha256": receipt_digest, "projection_status": "pending"},
            )
            known = set()
        if not {provider_event_id, success_event_id}.issubset(known):
            for event in events:
                event_id = event["payload"]["green500_event_id"]
                if event_id in known:
                    continue
                _append_event(destination, event)
                known.add(event_id)
                appended.append(event_id)
            _write_receipt_marker(
                marker,
                {
                    "receipt_sha256": receipt_digest,
                    "event_ids": [provider_event_id, success_event_id],
                    "provider_timestamp": provider_time,
                    "recorded_at": recorded_timestamp,
                    "projection_status": "complete",
                },
            )
        fcntl.flock(lock, fcntl.LOCK_UN)

    log_fields = {
        "ai_function": AI_FUNCTION,
        "project": PROJECT,
        "operation": OPERATION,
        "provider": provider_name,
        "plan": plan_name,
        "model": model,
        "effort": effort,
        "provider_request_id": request_id,
        "elapsed_ms": elapsed_ms,
        "usage": {
            key: value for key, value in usage.items() if key.startswith("usage_")
        },
        "cost_cny": cost_fields.get("cost_cny"),
        "cost_basis": cost_fields.get("cost_basis"),
        "provider_timestamp": provider_time,
        "recorded_at": recorded_timestamp,
        "appended_event_count": len(appended),
    }
    _LOGGER.info(
        "green500 llm benchmark observation %s",
        json.dumps(log_fields, sort_keys=True, separators=(",", ":")),
    )
    return {
        "call_id": call_id,
        "usage_event_id": usage_event_id,
        "appended_event_count": len(appended),
        "receipt_sha256": receipt_digest,
    }
