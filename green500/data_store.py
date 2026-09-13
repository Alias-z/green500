"""Store source bytes, typed observations and every collection outcome independently."""

import hashlib
import json

from psycopg.types.json import Jsonb

from green500 import db
from green500.storage import save_bytes


def save_dataset(
    settings, source_key, url, body, content_type, company_cik=None, task_id=None
):
    """Retain immutable provider bytes and register a repeatable dataset version."""
    digest = save_bytes(settings.data_dir, body)
    with db.connect(settings) as conn:
        if task_id is not None:
            db.require_active_task(conn, task_id)
        document_id = db.record_document(
            conn,
            company_cik=company_cik,
            url=url,
            final_url=url,
            title=source_key,
            kind="dataset",
            digest=digest,
            byte_count=len(body),
            content_type=content_type,
        )
        return conn.execute(
            "UPDATE documents SET source_key=%s,parse_status='succeeded' WHERE id=%s RETURNING id,sha256,url,last_checked_at",
            (source_key, document_id),
        ).fetchone()


def save_observations(
    settings,
    source_key,
    company_cik,
    dataset_id,
    observations,
    match_method="identifier",
    task_id=None,
):
    """Publish deterministic records without creating fictitious model calls."""
    with db.connect(settings) as conn:
        if task_id is not None:
            db.require_active_task(conn, task_id)
        for observation in observations:
            fingerprint = hashlib.sha256(
                json.dumps(
                    [company_cik, source_key, match_method, observation],
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            conn.execute(
                """INSERT INTO structured_observations
                (company_cik,source_key,dataset_id,metric_code,value,unit,period_start,period_end,
                 period_type,published_at,source_record,match_method,fingerprint)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(fingerprint) DO UPDATE SET last_checked_at=now()""",
                (
                    company_cik,
                    source_key,
                    dataset_id,
                    observation["metric_code"],
                    Jsonb(observation["value"]),
                    observation.get("unit"),
                    observation.get("period_start"),
                    observation.get("period_end"),
                    observation["period_type"],
                    observation.get("published_at"),
                    Jsonb(observation.get("source_record", {})),
                    match_method,
                    fingerprint,
                ),
            )
    return len(observations)


def record_check(
    settings, source_key, company_cik, status, error=None, dataset_id=None, details=None
):
    """Append a visible outcome while retaining previously collected values on failure."""
    with db.connect(settings) as conn:
        conn.execute(
            "INSERT INTO source_checks(company_cik,source_key,status,error,dataset_id,details) VALUES (%s,%s,%s,%s,%s,%s)",
            (company_cik, source_key, status, error, dataset_id, Jsonb(details or {})),
        )
