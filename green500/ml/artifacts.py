"""Write content-bound model run artifacts and reject changed saved models."""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

RUN_MANIFEST_VERSION = "green500-model-run-v2"
TARGETS = {"esg", "csa"}
MODEL_FAMILIES = {"ebm", "catboost"}


def canonical_json_bytes(value: object) -> bytes:
    """Return deterministic JSON bytes without permitting non-finite numbers."""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash one regular artifact file."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Artifact must be a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_description(path: str | Path) -> dict[str, int | str]:
    path = Path(path)
    return {"sha256": sha256_file(path), "byte_count": path.stat().st_size}


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_bytes(body)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: str | Path, value: object) -> None:
    """Atomically replace one human-readable JSON artifact."""
    body = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode()
    _atomic_write(Path(path), body)


def write_json_lines_atomic(path: str | Path, rows: Iterable[dict]) -> None:
    """Atomically write deterministic JSON Lines in caller-provided row order."""
    body = b"".join(canonical_json_bytes(row) for row in rows)
    _atomic_write(Path(path), body)


def model_path(run_dir: str | Path, target: str, family: str) -> Path:
    if target not in TARGETS:
        raise ValueError(f"Unsupported model target: {target!r}.")
    if family not in MODEL_FAMILIES:
        raise ValueError(f"Unsupported model family: {family!r}.")
    return Path(run_dir) / "models" / target / family / "model.pkl"


def save_model_artifact(
    run_dir: str | Path, target: str, family: str, model: object
) -> dict:
    """Serialize an actual fitted estimator and return its manifest description."""
    path = model_path(run_dir, target, family)
    body = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
    _atomic_write(path, body)
    return {
        "path": path.relative_to(Path(run_dir)).as_posix(),
        **file_description(path),
    }


def _required_manifest_keys() -> set[str]:
    return {
        "version",
        "run_id",
        "created_at",
        "status",
        "dataset_snapshot_id",
        "dataset_manifest_sha256",
        "ordered_features",
        "categorical_features",
        "feature_categories",
        "active_features",
        "excluded_features",
        "ablation_results",
        "numeric_training_ranges",
        "known_industries",
        "models",
        "registry_sha256",
        "config_sha256",
        "split_sha256",
        "dependency_versions",
        "files",
        "targets",
        "blockers",
        "limitations",
    }


def write_run_manifest(run_dir: str | Path, manifest: dict[str, Any]) -> Path:
    """Publish the last file in a run after every referenced artifact is stable."""
    run_dir = Path(run_dir)
    missing = sorted(_required_manifest_keys() - set(manifest))
    if missing:
        raise ValueError(f"Run manifest is missing fields: {missing}")
    if manifest["version"] != RUN_MANIFEST_VERSION:
        raise ValueError("Run manifest has an unsupported version.")
    path = run_dir / "run_manifest.json"
    write_json_atomic(path, manifest)
    return path


def _safe_member(run_dir: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("Run artifact paths must be non-empty relative paths.")
    path = (run_dir / relative).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError as error:
        raise ValueError(f"Run artifact escapes its directory: {relative}") from error
    return path


def verify_file_manifest(run_dir: str | Path, manifest: dict[str, Any]) -> None:
    """Verify every byte count and SHA-256 recorded by a run manifest."""
    run_dir = Path(run_dir)
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise TypeError("Run manifest files must be a mapping.")
    for relative, expected in files.items():
        path = _safe_member(run_dir, relative)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"Run artifact is missing: {relative}")
        if not isinstance(expected, dict) or set(expected) != {"sha256", "byte_count"}:
            raise ValueError(f"Run artifact description is invalid: {relative}")
        if file_description(path) != expected:
            raise ValueError(f"Run artifact content changed: {relative}")


def load_verified_run(run_dir: str | Path) -> dict[str, Any]:
    """Load one model run only after all declared artifacts and model links verify."""
    run_dir = Path(run_dir)
    path = run_dir / "run_manifest.json"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Run manifest does not exist: {path}")
    try:
        manifest = json.loads(path.read_bytes())
    except json.JSONDecodeError as error:
        raise ValueError("Run manifest is invalid JSON.") from error
    if not isinstance(manifest, dict):
        raise TypeError("Run manifest must be a JSON object.")
    missing = sorted(_required_manifest_keys() - set(manifest))
    if missing:
        raise ValueError(f"Run manifest is missing fields: {missing}")
    if manifest["version"] != RUN_MANIFEST_VERSION:
        raise ValueError("Run manifest has an unsupported version.")
    if manifest["run_id"] != run_dir.name:
        raise ValueError("Run manifest identifier does not match its directory.")
    verify_file_manifest(run_dir, manifest)
    ordered_features = manifest["ordered_features"]
    if not isinstance(ordered_features, list) or len(ordered_features) != len(
        set(ordered_features)
    ):
        raise ValueError("Run manifest ordered_features must be unique.")
    target_keys = set(manifest["models"])
    if (
        set(manifest["active_features"]) != target_keys
        or set(manifest["excluded_features"]) != target_keys
        or set(manifest["ablation_results"]) != target_keys
    ):
        raise ValueError(
            "Run manifest target feature artifacts differ from its models."
        )
    for target, active_features in manifest["active_features"].items():
        if (
            target not in TARGETS
            or not isinstance(active_features, list)
            or not active_features
        ):
            raise ValueError("Run manifest has an invalid target feature mask.")
        if len(active_features) != len(set(active_features)):
            raise ValueError("Run manifest active features contain duplicates.")
        expected_order = [
            feature for feature in ordered_features if feature in set(active_features)
        ]
        if active_features != expected_order:
            raise ValueError(
                "Run manifest active features must be an ordered subset of the registry."
            )
        exclusions = manifest["excluded_features"][target]
        if not isinstance(exclusions, dict) or set(exclusions) != set(
            ordered_features
        ) - set(active_features):
            raise ValueError(
                "Run manifest excluded features must complement its active mask."
            )
        if any(
            not isinstance(details, dict)
            or not isinstance(details.get("reasons"), list)
            or not details["reasons"]
            or not isinstance(details.get("support"), dict)
            for details in exclusions.values()
        ):
            raise ValueError(
                "Run manifest exclusions need reasons and support details."
            )
        ablations = manifest["ablation_results"][target]
        if not isinstance(ablations, dict) or set(ablations) != {
            "core",
            "core_plus_climate_target",
            "core_plus_financial_target",
            "core_plus_both",
        }:
            raise ValueError("Run manifest category ablation results are incomplete.")
    try:
        config = json.loads((run_dir / "config.json").read_bytes())
        dataset_manifest = json.loads((run_dir / "dataset_manifest.json").read_bytes())
    except json.JSONDecodeError as error:
        raise ValueError("A hash-bound run input is invalid JSON.") from error
    if sha256_bytes(canonical_json_bytes(config)) != manifest["config_sha256"]:
        raise ValueError("Run configuration hash does not match its parsed content.")
    if sha256_file(run_dir / "feature_registry.json") != manifest["registry_sha256"]:
        raise ValueError("Run feature registry hash does not match its content.")
    if (
        sha256_file(run_dir / "dataset_manifest.json")
        != manifest["dataset_manifest_sha256"]
    ):
        raise ValueError("Run dataset manifest hash does not match its content.")
    if dataset_manifest.get("snapshot_id") != manifest["dataset_snapshot_id"]:
        raise ValueError("Run dataset identifier differs from its saved manifest.")
    for target, expected_hash in manifest["split_sha256"].items():
        split_path = run_dir / "split_assignments" / f"{target}.json"
        try:
            split = json.loads(split_path.read_bytes())
        except json.JSONDecodeError as error:
            raise ValueError(f"Run split is invalid JSON: {target}") from error
        if sha256_bytes(canonical_json_bytes(split)) != expected_hash:
            raise ValueError(f"Run split hash does not match its content: {target}")
    for target, families in manifest["models"].items():
        if target not in TARGETS or not isinstance(families, dict):
            raise ValueError("Run manifest has an invalid model target.")
        for family, description in families.items():
            if family not in MODEL_FAMILIES or not isinstance(description, dict):
                raise ValueError("Run manifest has an invalid model family.")
            expected = (
                model_path(run_dir, target, family).relative_to(run_dir).as_posix()
            )
            if description.get("path") != expected:
                raise ValueError(
                    "Run manifest model path differs from the fixed layout."
                )
            if manifest["files"].get(expected) != {
                "sha256": description.get("sha256"),
                "byte_count": description.get("byte_count"),
            }:
                raise ValueError(
                    "Run manifest model description differs from its file inventory."
                )
    return manifest


def load_model_artifact(run_dir: str | Path, target: str, family: str) -> object:
    """Return one fitted estimator after run and model hashes verify."""
    run_dir = Path(run_dir)
    manifest = load_verified_run(run_dir)
    description = manifest["models"].get(target, {}).get(family)
    if description is None:
        raise FileNotFoundError(
            f"Model is unavailable for target={target}, family={family}."
        )
    path = model_path(run_dir, target, family)
    if sha256_file(path) != description["sha256"]:
        raise ValueError(
            "Saved model content changed after the run manifest was written."
        )
    try:
        with path.open("rb") as stream:
            return pickle.load(stream)
    except (pickle.UnpicklingError, AttributeError, EOFError, ImportError) as error:
        raise ValueError(f"Saved model cannot be loaded: {path}") from error


def publish_run_directory(temporary: Path, final: Path) -> None:
    """Publish one complete run without overwriting an existing run identifier."""
    if final.exists():
        raise FileExistsError(f"Model run already exists: {final}")
    try:
        temporary.replace(final)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def write_latest_run_pointer(output_dir: str | Path, run_dir: str | Path) -> Path:
    """Point inference at one fully published, hash-bound model run."""
    output_dir = Path(output_dir).resolve()
    run_dir = Path(run_dir).resolve()
    try:
        relative = run_dir.relative_to(output_dir).as_posix()
    except ValueError as error:
        raise ValueError(
            "Latest model run must be beneath its output directory."
        ) from error
    manifest_path = run_dir / "run_manifest.json"
    manifest = load_verified_run(run_dir)
    pointer = {
        "version": RUN_MANIFEST_VERSION,
        "run_id": manifest["run_id"],
        "run_path": relative,
        "run_manifest_sha256": sha256_file(manifest_path),
    }
    path = output_dir / "latest.json"
    write_json_atomic(path, pointer)
    return path
