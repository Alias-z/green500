"""Load explicitly authorized ESG and CSA labels from a documented template."""

from __future__ import annotations

import csv
from pathlib import Path

from pydantic import ValidationError

from green500.ml.contracts import LabelRecord

LABEL_TEMPLATE_COLUMNS = (
    "company_cik",
    "target_name",
    "assessment_cycle",
    "prediction_as_of",
    "score",
    "label_published_at",
    "source_name",
    "source_url",
    "source_date",
    "source_sha256",
    "authorization_reference",
)


def _parse_label_row(row: dict[str, str], row_number: int) -> LabelRecord:
    """Convert one CSV row into the strict authorized-label contract."""
    try:
        return LabelRecord.model_validate(
            {
                **row,
                "company_cik": row["company_cik"].zfill(10),
                "score": float(row["score"]),
            },
            strict=False,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise ValueError(f"Invalid authorized label at CSV row {row_number}: {error}") from error


def load_authorized_labels(path: Path | str) -> list[LabelRecord]:
    """Load labels only from the explicit long-form authorized import file."""
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != LABEL_TEMPLATE_COLUMNS:
            raise ValueError(
                "Authorized label CSV columns must exactly match the published template."
            )
        labels = [
            _parse_label_row(row, row_number)
            for row_number, row in enumerate(reader, start=2)
            if any((value or "").strip() for value in row.values())
        ]
    identities = set()
    for label in labels:
        identity = (
            label.company_cik,
            label.assessment_cycle,
            label.prediction_as_of,
            label.target_name,
        )
        if identity in identities:
            raise ValueError(
                "Authorized label CSV contains a duplicate company, cycle, date, and target."
            )
        identities.add(identity)
    return labels


def write_label_template(path: Path | str) -> Path:
    """Write an empty label-import template without inventing score examples."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream, lineterminator="\n").writerow(LABEL_TEMPLATE_COLUMNS)
    return path
