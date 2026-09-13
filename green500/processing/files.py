"""Small file-persistence helpers shared by standalone processing commands."""

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path


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


_SAFE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def require_safe_path_segment(value, subject):
    """Return one bounded ASCII path segment or reject traversal and separators."""
    if not isinstance(value, str) or _SAFE_PATH_SEGMENT.fullmatch(value) is None:
        raise ValueError(
            f"{subject} must contain only letters, numbers, dots, underscores and hyphens."
        )
    return value


def completion_client_identity(client):
    """Return the request settings that determine a model completion result."""
    model = getattr(client, "model", None)
    endpoint = getattr(client, "base_url", None)
    options = getattr(client, "extra_body", {})
    if not isinstance(model, str) or not model:
        raise ValueError("Completion client model is required.")
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("Completion client endpoint is required.")
    try:
        canonical_options = json.loads(
            json.dumps(options, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Completion client options must be valid JSON.") from error
    return {"model": model, "endpoint": endpoint, "options": canonical_options}


def reusable_successful_receipt(
    result_directory, output_filename, *, required_filenames=()
):
    """Return a hash-verified successful receipt without replacing its evidence."""
    result_directory = Path(result_directory)
    receipt_path = result_directory / "receipt.json"
    if not receipt_path.exists():
        return None
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("status") != "succeeded":
        return None
    output_path = result_directory / output_filename
    required_paths = [
        output_path,
        *(result_directory / name for name in required_filenames),
    ]
    missing = [path.name for path in required_paths if not path.is_file()]
    if missing:
        raise ValueError(f"Successful result is missing artifacts: {sorted(missing)}")
    expected_hash = receipt.get("output_sha256")
    actual_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
    if expected_hash != actual_hash:
        raise ValueError("Successful result output failed SHA-256 verification.")
    return {**receipt, "reused": True}


def retained_unknown_receipt(result_directory):
    """Keep an indeterminate provider outcome from starting another paid request."""
    receipt_path = Path(result_directory) / "receipt.json"
    if not receipt_path.exists():
        return None
    receipt = json.loads(receipt_path.read_text())
    return {**receipt, "reused": True} if receipt.get("status") == "unknown" else None


def create_attempt_directory(result_directory):
    """Create an immutable attempt directory and retain any legacy top-level attempt."""
    result_directory = Path(result_directory)
    attempts_directory = result_directory / "attempts"
    attempts_directory.mkdir(parents=True, exist_ok=True)
    receipt_path = result_directory / "receipt.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if not receipt.get("attempt_id"):
            legacy_directory = attempts_directory / f"legacy-{time.time_ns()}"
            legacy_directory.mkdir()
            for path in result_directory.iterdir():
                if path.is_file():
                    shutil.copy2(path, legacy_directory / path.name)
    while True:
        attempt_id = str(time.time_ns())
        attempt_directory = attempts_directory / attempt_id
        try:
            attempt_directory.mkdir()
        except FileExistsError:
            continue
        return attempt_id, attempt_directory


def publish_attempt(result_directory, attempt_directory, *, receipt_only=False):
    """Atomically publish one archived attempt, writing its receipt last."""
    result_directory = Path(result_directory)
    attempt_directory = Path(attempt_directory)
    names = (
        ["receipt.json"]
        if receipt_only
        else [path.name for path in attempt_directory.iterdir() if path.is_file()]
    )
    names = sorted(name for name in names if name != "receipt.json") + ["receipt.json"]
    for name in names:
        source = attempt_directory / name
        if not source.is_file():
            continue
        destination = result_directory / name
        temporary = destination.with_name(
            destination.name + f".{os.getpid()}.{time.time_ns()}.tmp"
        )
        temporary.write_bytes(source.read_bytes())
        temporary.replace(destination)
