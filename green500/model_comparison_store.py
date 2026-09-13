"""Publish verified model rows and query active measurements in PostgreSQL."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

from psycopg.types.json import Jsonb

from green500 import db


def current_snapshot(settings) -> dict:
    with db.connect(settings) as connection:
        record = connection.execute(
            "SELECT * FROM model_comparison_snapshots WHERE is_current"
        ).fetchone()
    if not record:
        raise FileNotFoundError("No company comparison snapshot has been published.")
    return record


def _verified_evidence(connection, rows: list[dict]) -> list[dict]:
    """Only expose file links bound to the original document identity and hash."""
    rows = copy.deepcopy(rows)
    document_ids = sorted(
        {
            metadata["source_document_id"]
            for row in rows
            for metadata in row.get("metadata", {}).values()
            if isinstance(metadata.get("source_document_id"), int)
        }
    )
    documents = {
        row["id"]: row
        for row in connection.execute(
            "SELECT id, company_cik, sha256 FROM documents WHERE id=ANY(%s)",
            (document_ids,),
        ).fetchall()
    }
    for row in rows:
        for metadata in row.get("metadata", {}).values():
            metadata.pop("source_view_url", None)
            document_id = metadata.get("source_document_id")
            if document_id is None:
                continue
            document = documents.get(document_id)
            if (
                document
                and document["company_cik"] == row["company"]["company_cik"]
                and document["sha256"] == metadata.get("source_document_sha256")
            ):
                metadata["source_view_url"] = f"/api/catalog/files/{document_id}/view"
            else:
                metadata["source_document_id"] = None
                metadata["source_view_url"] = None
                metadata["evidence_warning"] = (
                    "Original document identity or hash could not be verified."
                )
    return rows


def publish_snapshot(settings, service, rows: list[dict]) -> dict:
    """Atomically install complete feature rows and both target evaluations."""
    if not rows:
        raise ValueError("Publication needs at least one company.")
    identifiers = [row["company"]["company_cik"] for row in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Publication contains duplicate companies.")
    dates = {row["prediction_as_of"] for row in rows}
    if len(dates) != 1:
        raise ValueError("Publication rows must share a prediction date.")
    with db.connect(settings) as connection:
        rows = _verified_evidence(connection, rows)
    description = service.describe()
    from green500.ml.artifacts import sha256_file

    description["run_manifest_sha256"] = sha256_file(
        service.run_dir / "run_manifest.json"
    )
    evaluations = {
        target: service.evaluate_rows(rows, target) for target in ("esg", "csa")
    }
    snapshot_id = hashlib.sha256(
        json.dumps(
            {"description": description, "rows": rows, "evaluations": evaluations},
            sort_keys=True,
            allow_nan=False,
            default=str,
        ).encode()
    ).hexdigest()
    summary = {
        **description,
        "snapshot_id": snapshot_id,
        "prediction_as_of": next(iter(dates)),
        "company_count": len(rows),
    }
    with db.connect(settings) as connection:
        connection.execute(
            Path(__file__).with_name("model_comparison_schema.sql").read_text()
        )
        connection.execute("SELECT pg_advisory_xact_lock(75002193)")
        exists = connection.execute(
            "SELECT 1 FROM model_comparison_snapshots WHERE snapshot_id=%s",
            (snapshot_id,),
        ).fetchone()
        if not exists:
            connection.execute(
                "INSERT INTO model_comparison_snapshots(snapshot_id,run_id,run_directory,prediction_as_of,summary,company_count) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    snapshot_id,
                    description["run_id"],
                    str(service.run_dir),
                    next(iter(dates)),
                    Jsonb(summary),
                    len(rows),
                ),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO model_comparison_company_rows VALUES (%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            snapshot_id,
                            row["company"]["company_cik"],
                            row["company"].get(
                                "company_name", row["company"].get("name", "")
                            ),
                            row["company"].get("ticker", ""),
                            row["company"].get("industry") or "Unknown",
                            Jsonb(row),
                        )
                        for row in rows
                    ],
                )
                for target, items in evaluations.items():
                    if len(items) != len(rows):
                        raise ValueError(
                            "Model evaluation returned an incomplete company set."
                        )
                    for item in items:
                        reconstructed = (
                            item["intercept"]
                            + item["fixed_context_contribution"]
                            + sum(item["category_contributions"].values())
                        )
                        if not math.isclose(
                            reconstructed,
                            item["predictions"]["ebm"],
                            rel_tol=1e-9,
                            abs_tol=1e-8,
                        ):
                            raise ValueError(
                                "Published EBM contributions do not reconstruct the estimate."
                            )
                    cursor.executemany(
                        "INSERT INTO model_comparison_predictions VALUES (%s,%s,%s,%s,%s,%s)",
                        [
                            (
                                snapshot_id,
                                item["company"]["company_cik"],
                                target,
                                item["predictions"]["ebm"],
                                item["predictions"]["catboost"],
                                Jsonb(item),
                            )
                            for item in items
                        ],
                    )
                    active = set(description["targets"][target]["active_features"])
                    values = []
                    for item in items:
                        if set(item["features"]) != active:
                            raise ValueError(
                                "Published fields differ from the active model contract."
                            )
                        for name, value in item["features"].items():
                            numeric = (
                                float(value)
                                if isinstance(value, (float, int))
                                else None
                            )
                            values.append(
                                (
                                    snapshot_id,
                                    item["company"]["company_cik"],
                                    target,
                                    name,
                                    service.registry[name]["category_or_context"],
                                    numeric,
                                    value if isinstance(value, str) else None,
                                )
                            )
                    cursor.executemany(
                        "INSERT INTO model_comparison_feature_values VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        values,
                    )
        connection.execute(
            "UPDATE model_comparison_snapshots SET is_current=false WHERE is_current"
        )
        connection.execute(
            "UPDATE model_comparison_snapshots SET is_current=true WHERE snapshot_id=%s",
            (snapshot_id,),
        )
    return summary


def query_companies(
    settings,
    snapshot: dict,
    *,
    target="esg",
    search="",
    industry=None,
    filters=(),
    sort_by="company_name",
    direction="asc",
    limit=500,
) -> dict:
    """Run only allowlisted active feature filters and ordering in PostgreSQL."""
    active = snapshot["summary"]["targets"][target]["active_features"]
    if sort_by not in {"company_name", "ebm", "catboost", *active}:
        raise ValueError(
            "Sorting is limited to company names, estimates and this target's active fields."
        )
    if direction not in {"asc", "desc"}:
        raise ValueError("Sorting direction must be asc or desc.")
    conditions = ["p.snapshot_id=%s", "p.target=%s"]
    parameters = [snapshot["snapshot_id"], target]
    if search:
        conditions.append(
            "(c.company_name ILIKE %s OR c.ticker ILIKE %s OR c.company_cik ILIKE %s)"
        )
        parameters += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if industry:
        conditions.append("c.industry=%s")
        parameters.append(industry)
    for index, condition in enumerate(filters):
        name = condition["feature_name"]
        if name not in active or name == "industry":
            raise ValueError("Numeric filters require an active numerical model field.")
        alias = f"f{index}"
        clauses = [
            f"{alias}.snapshot_id=p.snapshot_id",
            f"{alias}.company_cik=p.company_cik",
            f"{alias}.target=p.target",
            f"{alias}.feature_name=%s",
        ]
        parameters.append(name)
        for bound, operator in (("minimum", ">="), ("maximum", "<=")):
            if condition.get(bound) is not None:
                value = condition[bound]
                if isinstance(value, bool) or not math.isfinite(value):
                    raise ValueError("Filter bounds must be finite numbers.")
                clauses.append(f"{alias}.numeric_value {operator} %s")
                parameters.append(value)
        if condition.get("minimum") is None and condition.get("maximum") is None:
            raise ValueError("A numeric filter needs at least one bound.")
        if (
            condition.get("minimum") is not None
            and condition.get("maximum") is not None
            and condition["minimum"] > condition["maximum"]
        ):
            raise ValueError("Filter minimum cannot exceed maximum.")
        conditions.append(
            f"EXISTS (SELECT 1 FROM model_comparison_feature_values {alias} WHERE {' AND '.join(clauses)})"
        )
    if sort_by == "company_name":
        ordering = 'LOWER(c.company_name) COLLATE "C"'
    elif sort_by in {"ebm", "catboost"}:
        ordering = f"p.{sort_by}"
    elif sort_by == "industry":
        ordering = "c.industry"
    else:
        ordering = "(SELECT numeric_value FROM model_comparison_feature_values sf WHERE sf.snapshot_id=p.snapshot_id AND sf.company_cik=p.company_cik AND sf.target=p.target AND sf.feature_name=%s)"
        parameters.append(sort_by)
    query = f"""SELECT p.evaluation, COUNT(*) OVER() AS total
        FROM model_comparison_predictions p JOIN model_comparison_company_rows c
        USING(snapshot_id, company_cik) WHERE {" AND ".join(conditions)}
        ORDER BY {ordering} {direction} NULLS LAST, p.company_cik LIMIT %s"""
    parameters.append(limit)
    with db.connect(settings) as connection:
        records = connection.execute(query, parameters).fetchall()
    return {
        "companies": [record["evaluation"] for record in records],
        "total": records[0]["total"] if records else 0,
    }


def source_rows(settings, snapshot_id: str, company_ids: list[str]) -> list[dict]:
    with db.connect(settings) as connection:
        records = connection.execute(
            "SELECT company_cik,source_row FROM model_comparison_company_rows WHERE snapshot_id=%s AND company_cik=ANY(%s)",
            (snapshot_id, company_ids),
        ).fetchall()
    rows = {record["company_cik"]: record["source_row"] for record in records}
    if set(company_ids) != set(rows):
        raise KeyError("A selected company is absent from this published snapshot.")
    return [rows[company_id] for company_id in company_ids]


def observed_feature_values(
    settings, snapshot_id: str, target: str
) -> dict[str, list[float]]:
    """Return current numeric values used to search realistic one-field scenarios."""
    with db.connect(settings) as connection:
        records = connection.execute(
            """SELECT feature_name,numeric_value
            FROM model_comparison_feature_values
            WHERE snapshot_id=%s AND target=%s AND numeric_value IS NOT NULL
            ORDER BY feature_name,numeric_value""",
            (snapshot_id, target),
        ).fetchall()
    values: dict[str, list[float]] = {}
    for record in records:
        values.setdefault(record["feature_name"], []).append(record["numeric_value"])
    return values


def published_ranking_values(settings, snapshot_id: str, target: str) -> list[dict]:
    """Read only the stored EBM values needed for live category reweighting."""
    with db.connect(settings) as connection:
        return connection.execute(
            """SELECT c.company_cik,c.company_name,c.ticker,c.industry,p.ebm,
                (p.evaluation->>'intercept')::double precision AS intercept,
                (p.evaluation->>'fixed_context_contribution')::double precision
                    AS fixed_context_contribution,
                p.evaluation->'category_contributions' AS category_contributions
            FROM model_comparison_predictions p
            JOIN model_comparison_company_rows c USING(snapshot_id,company_cik)
            WHERE p.snapshot_id=%s AND p.target=%s
            ORDER BY LOWER(c.company_name) COLLATE "C",c.company_cik""",
            (snapshot_id, target),
        ).fetchall()


def published_scenario_rows(settings, snapshot_id: str, target: str) -> list[dict]:
    """Read compact feature rows and original predictions for an all-company scenario."""
    with db.connect(settings) as connection:
        records = connection.execute(
            """SELECT c.company_cik,c.company_name,c.ticker,c.industry,p.ebm,p.catboost,
                c.source_row->'features' AS features,
                c.source_row->>'availability_policy' AS availability_policy
            FROM model_comparison_predictions p
            JOIN model_comparison_company_rows c USING(snapshot_id,company_cik)
            WHERE p.snapshot_id=%s AND p.target=%s
            ORDER BY LOWER(c.company_name) COLLATE "C",c.company_cik""",
            (snapshot_id, target),
        ).fetchall()
    return [
        {
            "company": {
                "company_cik": record["company_cik"],
                "name": record["company_name"],
                "ticker": record["ticker"],
                "industry": record["industry"],
            },
            "features": record["features"],
            "availability_policy": record["availability_policy"],
            "original_predictions": {
                "ebm": record["ebm"],
                "catboost": record["catboost"],
            },
        }
        for record in records
    ]
