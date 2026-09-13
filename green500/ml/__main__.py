"""Command-line entry points for Green500 model data, training, and inference."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

AVAILABILITY_POLICY_CHOICES = (
    "publisher-publication-date",
    "public-document-acquisition-fallback",
)


def _add_availability_policy_argument(command: argparse.ArgumentParser) -> None:
    """Add the shared source-availability policy without changing publication dates."""
    command.add_argument(
        "--availability-policy",
        choices=AVAILABILITY_POLICY_CHOICES,
        default="publisher-publication-date",
    )


def _json_object(value: str, subject: str) -> dict:
    if value.startswith("@"):
        body = Path(value[1:]).read_text()
    else:
        body = value
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise ValueError(f"{subject} must be a JSON object or @file path.") from error
    if not isinstance(parsed, dict):
        raise TypeError(f"{subject} must be a JSON object.")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit", help="Audit dated feature and label coverage.")
    audit.add_argument("--config", type=Path, default=Path("config/ml.yaml"))
    audit.add_argument("--labels", type=Path)
    audit.add_argument(
        "--repair-output",
        type=Path,
        help="Write full read-only source metadata repair rows to this JSON file.",
    )
    _add_availability_policy_argument(audit)

    build = commands.add_parser(
        "build-dataset", help="Build one immutable dated dataset snapshot."
    )
    build.add_argument("--config", type=Path, default=Path("config/ml.yaml"))
    build.add_argument("--labels", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    _add_availability_policy_argument(build)

    train = commands.add_parser(
        "train", help="Select and fit eligible EBM and CatBoost models."
    )
    train.add_argument("--config", type=Path, default=Path("config/ml.yaml"))
    train.add_argument("--dataset-dir", type=Path, required=True)
    train.add_argument("--output-dir", type=Path)
    train.add_argument("--split-mode", choices=("historical", "snapshot"))

    predict = commands.add_parser(
        "predict", help="Predict one company from a saved model run."
    )
    predict.add_argument("company_id")
    predict.add_argument("--prediction-as-of", required=True)
    predict.add_argument("--model-run", type=Path)
    predict.add_argument("--assessment-cycle", default="inference")

    scenario = commands.add_parser(
        "scenario", help="Compare saved-model predictions after feature overrides."
    )
    scenario.add_argument("company_id")
    scenario.add_argument("--overrides", required=True)
    scenario.add_argument("--prediction-as-of", required=True)
    scenario.add_argument("--model-run", type=Path)
    scenario.add_argument("--assessment-cycle", default="inference")

    rank = commands.add_parser(
        "rank", help="Rank a cohort with signed EBM category contributions."
    )
    rank.add_argument("--weights", default="{}")
    rank.add_argument("--target", choices=("esg", "csa"), default="esg")
    rank.add_argument("--prediction-as-of", required=True)
    rank.add_argument("--model-run", type=Path)
    rank.add_argument("--assessment-cycle", default="inference")
    cohort = rank.add_mutually_exclusive_group()
    cohort.add_argument("--company-ids", help="Comma-separated company identifiers.")
    cohort.add_argument("--industry")

    pipeline = commands.add_parser(
        "pipeline",
        help="Audit, build a dated snapshot, and train only when labels and splits allow it.",
    )
    pipeline.add_argument("--config", type=Path, default=Path("config/ml.yaml"))
    pipeline.add_argument("--labels", type=Path, required=True)
    pipeline.add_argument("--output-dir", type=Path, required=True)
    pipeline.add_argument("--split-mode", choices=("historical", "snapshot"))
    _add_availability_policy_argument(pipeline)
    return parser


def _check_config(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError("ML configuration file does not exist: " + str(path))


def _run(args: argparse.Namespace) -> dict:
    if args.command == "train":
        _check_config(args.config)
        from green500.ml.training import train_models

        return train_models(
            args.dataset_dir,
            args.config,
            output_dir=args.output_dir,
            split_mode=args.split_mode,
        )

    from green500.config import load_settings

    settings = load_settings()
    if args.command == "audit":
        _check_config(args.config)
        from green500.ml.audit import audit_data

        return audit_data(
            settings,
            args.labels,
            args.config,
            args.repair_output,
            args.availability_policy,
        )
    if args.command == "build-dataset":
        _check_config(args.config)
        from green500.ml.dataset import build_dataset_snapshot

        return build_dataset_snapshot(
            settings, args.labels, args.output_dir, args.availability_policy
        )
    if args.command == "predict":
        from green500.ml.inference import predict_company

        return predict_company(
            args.company_id,
            args.prediction_as_of,
            settings=settings,
            model_run=args.model_run,
            assessment_cycle=args.assessment_cycle,
        )
    if args.command == "scenario":
        from green500.ml.scenario import predict_scenario

        return predict_scenario(
            args.company_id,
            _json_object(args.overrides, "overrides"),
            args.prediction_as_of,
            settings=settings,
            model_run=args.model_run,
            assessment_cycle=args.assessment_cycle,
        )
    if args.command == "rank":
        from green500.ml.personalization import rank_companies

        cohort = None
        if args.company_ids:
            cohort = [value.strip() for value in args.company_ids.split(",") if value.strip()]
        elif args.industry:
            cohort = {"industry": args.industry}
        return rank_companies(
            _json_object(args.weights, "weights"),
            target=args.target,
            cohort=cohort,
            prediction_as_of=args.prediction_as_of,
            settings=settings,
            model_run=args.model_run,
            assessment_cycle=args.assessment_cycle,
        )
    if args.command == "pipeline":
        _check_config(args.config)
        from green500.ml.audit import audit_data
        from green500.ml.dataset import build_dataset_snapshot
        from green500.ml.training import train_models

        args.output_dir.mkdir(parents=True, exist_ok=True)
        audit_result = audit_data(
            settings,
            args.labels,
            args.config,
            availability_policy=args.availability_policy,
        )
        dataset_result = build_dataset_snapshot(
            settings,
            args.labels,
            args.output_dir / "datasets",
            args.availability_policy,
        )
        dataset_dir = dataset_result.get("snapshot_dir")
        if not dataset_dir:
            raise ValueError("Dataset builder returned no snapshot_dir.")
        label_counts = dataset_result.get("label_available_counts") or {}
        if not any(label_counts.get(target, 0) for target in ("esg", "csa")):
            reason = "No authorized dated ESG or CSA labels are available."
            training_result = {
                "status": "blocked",
                "run_id": None,
                "run_dir": None,
                "dataset_snapshot_id": dataset_result.get("snapshot_id"),
                "targets": {
                    target: {"status": "blocked", "reason": reason}
                    for target in ("esg", "csa")
                },
                "blockers": [reason],
                "training_skipped": True,
            }
        else:
            training_result = train_models(
                dataset_dir,
                args.config,
                output_dir=args.output_dir / "runs",
                split_mode=args.split_mode,
            )
        return {
            "status": (
                "blocked"
                if training_result.get("status") == "blocked"
                else "completed"
            ),
            "audit": audit_result,
            "dataset": dataset_result,
            "training": training_result,
            "report_extraction_started": False,
        }
    raise RuntimeError("Unknown ML command.")


def main() -> int:
    args = _parser().parse_args()
    try:
        result = _run(args)
    except (FileNotFoundError, RuntimeError, TypeError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
