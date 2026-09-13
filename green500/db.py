"""Small PostgreSQL transactions for collection, processing and the Ops table."""

import hashlib
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from green500.config import Settings


def connect(settings: Settings):
    """Open a bounded connection; never reuse a connection across concurrent work."""
    return psycopg.connect(
        settings.database_url,
        row_factory=dict_row,
        connect_timeout=5,
        options="-c statement_timeout=15000 -c lock_timeout=5000 -c application_name=green500",
    )


def initialize_database(settings: Settings) -> None:
    """Apply the initial schema only through the explicit setup command."""
    with connect(settings) as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())


def get_task(settings: Settings, task_id: int) -> dict:
    """Return an existing task or a clear missing-record error."""
    with connect(settings) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=%s", (task_id,)).fetchone()
    if not row:
        raise ValueError("Task does not exist.")
    return row


def enqueue(settings: Settings, kind: str, company_cik: str | None, value: dict) -> int:
    """Deduplicate active requests while allowing a later explicit refresh."""
    input_key = hashlib.sha256(
        json.dumps([kind, company_cik, value], sort_keys=True).encode()
    ).hexdigest()
    with connect(settings) as conn:
        row = conn.execute(
            """INSERT INTO tasks(kind,company_cik,input,input_key) VALUES (%s,%s,%s,%s)
            ON CONFLICT(input_key) WHERE status IN ('pending','running')
            DO UPDATE SET input_key=EXCLUDED.input_key RETURNING id""",
            (kind, company_cik, Jsonb(value), input_key),
        ).fetchone()
    return row["id"]


def claim(settings: Settings, task_id: int | None = None) -> dict | None:
    """Claim one queued task; keep a finite execution deadline and exact attempt record."""
    with connect(settings) as conn:
        expired = conn.execute("""UPDATE tasks SET status='unknown',error='Execution deadline passed; inspect before retry.',finished_at=now()
                        WHERE status='running' AND expires_at<now() RETURNING id""").fetchall()
        for old in expired:
            conn.execute(
                "UPDATE task_attempts SET status='unknown',finished_at=now() WHERE task_id=%s AND status='running'",
                (old["id"],),
            )
            conn.execute(
                "UPDATE extractions SET status='unknown',error='Worker ended without a model completion receipt.' WHERE task_id=%s AND status='running'",
                (old["id"],),
            )
        if conn.execute("SELECT is_paused FROM controls WHERE id=1").fetchone()[
            "is_paused"
        ]:
            return None
        task = conn.execute(
            """SELECT * FROM tasks WHERE status='pending' AND (%s::bigint IS NULL OR id=%s)
                               ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""",
            (task_id, task_id),
        ).fetchone()
        if task:
            conn.execute(
                "UPDATE tasks SET status='running',started_at=now(),expires_at=now()+%s*interval '1 second' WHERE id=%s",
                ((7200 if task["kind"] in {"collect_sec_reports","collect_sustainability_reports"} else settings.task_timeout_seconds) + 300, task["id"]),
            )
            task["attempt_id"] = conn.execute(
                "INSERT INTO task_attempts(task_id) VALUES (%s) RETURNING id",
                (task["id"],),
            ).fetchone()["id"]
        return task


def finish(
    settings: Settings,
    task_id: int,
    status: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    """Finish only the currently running attempt, retaining failures and previous outputs."""
    with connect(settings) as conn:
        changed = conn.execute(
            """UPDATE tasks SET status=%s,result=COALESCE(%s,result),error=%s,finished_at=now()
                                  WHERE id=%s AND status='running' RETURNING id""",
            (status, Jsonb(result) if result is not None else None, error, task_id),
        ).fetchone()
        if changed:
            conn.execute(
                """UPDATE task_attempts SET status=%s,detail=%s,finished_at=now()
                            WHERE task_id=%s AND status='running'""",
                (status, Jsonb({"result": result, "error": error}), task_id),
            )
            conn.execute(
                "UPDATE extractions SET status='unknown',error='Worker ended without a model completion receipt.' WHERE task_id=%s AND status='running'",
                (task_id,),
            )


def require_active_task(conn, task_id: int) -> None:
    """Lock and validate the publication authority of one non-reused task identity."""
    row = conn.execute(
        "SELECT id FROM tasks WHERE id=%s AND status='running' AND expires_at>now() FOR UPDATE",
        (task_id,),
    ).fetchone()
    if not row:
        raise ValueError("Task is no longer active; its output cannot be published.")


def record_document(
    conn,
    *,
    company_cik,
    url,
    final_url,
    title,
    kind,
    digest,
    byte_count,
    content_type,
    year=None,
    source_updated_at=None,
    revision=None,
) -> int:
    """Register an immutable byte version while preserving later observation times."""
    return conn.execute(
        """INSERT INTO documents(company_cik,url,final_url,title,kind,sha256,byte_count,content_type,reporting_year,source_updated_at,revision)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(company_cik,url,sha256,kind) DO UPDATE SET last_checked_at=now(),acquisition_method='scrapy'
        RETURNING id""",
        (
            company_cik,
            url,
            final_url,
            title,
            kind,
            digest,
            byte_count,
            content_type,
            year,
            source_updated_at,
            revision,
        ),
    ).fetchone()["id"]


def publish_memberships(
    settings: Settings, document: dict, rows: list[dict], task_id: int
) -> dict:
    """Publish every security and update the current company set in one transaction."""
    groups = {}
    for row in rows:
        groups.setdefault(row["cik"], []).append(row)
    with connect(settings) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(5002026)")
        require_active_task(conn, task_id)
        document_id = record_document(conn, **document)
        result_digest = hashlib.sha256(
            json.dumps(rows, sort_keys=True).encode()
        ).hexdigest()
        snapshot_id = conn.execute(
            """INSERT INTO index_snapshots(document_id,parser_version,result_digest)
            VALUES (%s,'constituents-v1',%s) ON CONFLICT(document_id,parser_version,result_digest)
            DO UPDATE SET result_digest=EXCLUDED.result_digest RETURNING id""",
            (document_id, result_digest),
        ).fetchone()["id"]
        conn.execute("UPDATE companies SET is_current=false")
        for cik, securities in groups.items():
            conn.execute(
                """INSERT INTO companies(cik,name,sector,symbols) VALUES (%s,%s,%s,%s)
                ON CONFLICT(cik) DO UPDATE SET name=EXCLUDED.name,sector=EXCLUDED.sector,
                symbols=EXCLUDED.symbols,is_current=true,updated_at=now()""",
                (
                    cik,
                    securities[0]["name"],
                    securities[0]["sector"],
                    sorted(r["symbol"] for r in securities),
                ),
            )
        for row in rows:
            conn.execute(
                """INSERT INTO index_memberships(document_id,snapshot_id,symbol,company_cik,source_row,source_fields)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(snapshot_id,symbol) DO NOTHING""",
                (
                    document_id,
                    snapshot_id,
                    row["symbol"],
                    row["cik"],
                    row["source_row"],
                    Jsonb(row),
                ),
            )
        conn.execute(
            "UPDATE documents SET parse_status='succeeded' WHERE id=%s", (document_id,)
        )
        conn.execute(
            "UPDATE controls SET current_index_document_id=%s,current_index_snapshot_id=%s WHERE id=1",
            (document_id, snapshot_id),
        )
        result = {
            "document_id": document_id,
            "snapshot_id": snapshot_id,
            "company_count": len(groups),
            "security_count": len(rows),
        }
        conn.execute(
            "UPDATE tasks SET status='succeeded',result=%s,finished_at=now() WHERE id=%s AND status='running'",
            (Jsonb(result), task_id),
        )
        conn.execute(
            "UPDATE task_attempts SET status='succeeded',detail=%s,finished_at=now() WHERE task_id=%s AND status='running'",
            (Jsonb(result), task_id),
        )
    return result


def company_rows(
    settings: Settings, search: str = "", year: int | None = None
) -> list[dict]:
    """Build one Ops row per company, with independently measured stage outcomes."""
    with connect(settings) as conn:
        return conn.execute(
            """SELECT c.*,
            (SELECT count(*) FROM documents d WHERE d.company_cik=c.cik AND d.kind IN ('report','feed') AND (%(year)s::int IS NULL OR d.reporting_year=%(year)s)) AS document_count,
            (SELECT count(*) FROM documents d WHERE d.company_cik=c.cik AND d.kind IN ('report','feed') AND d.parse_status='succeeded' AND (%(year)s::int IS NULL OR d.reporting_year=%(year)s)) AS parsed_count,
            (SELECT count(*) FROM analysis_metrics m WHERE m.company_cik=c.cik AND (%(year)s::int IS NULL OR m.reporting_year=%(year)s)) AS metric_count,
            (SELECT count(*) FROM extractions e JOIN documents d ON d.id=e.document_id WHERE d.company_cik=c.cik AND e.response_sha256 IS NOT NULL) AS model_call_count,
            (SELECT sum(COALESCE((e.usage->>'total_tokens')::bigint,0)) FROM extractions e JOIN documents d ON d.id=e.document_id WHERE d.company_cik=c.cik AND e.response_sha256 IS NOT NULL) AS total_tokens,
            (SELECT max(d.last_checked_at) FROM documents d WHERE d.company_cik=c.cik) AS last_collected_at,
            COALESCE((SELECT t.status FROM tasks t WHERE t.company_cik=c.cik AND t.kind='collect_source' ORDER BY t.id DESC LIMIT 1),'pending') AS collection_status,
            COALESCE((SELECT t.status FROM tasks t WHERE t.company_cik=c.cik AND t.kind='extract' ORDER BY t.id DESC LIMIT 1),'pending') AS extraction_status,
            (SELECT t.error FROM tasks t WHERE t.company_cik=c.cik ORDER BY t.id DESC LIMIT 1) AS latest_error
            ,COALESCE((SELECT jsonb_agg(to_jsonb(o)) FROM latest_observations o WHERE o.company_cik=c.cik AND (%(year)s::int IS NULL OR EXTRACT(year FROM o.period_end)=%(year)s)), '[]'::jsonb) AS observations
            ,COALESCE((SELECT jsonb_agg(to_jsonb(s)) FROM source_status s WHERE s.company_cik=c.cik), '[]'::jsonb) AS source_status
            FROM companies c WHERE c.is_current AND (c.name ILIKE %(search)s OR array_to_string(c.symbols,',') ILIKE %(search)s)
            ORDER BY c.name""",
            {"search": f"%{search}%", "year": year},
        ).fetchall()


def company_detail(settings: Settings, cik: str) -> dict:
    """Expose source documents, every attempt and cited metrics without credentials."""
    with connect(settings) as conn:
        return {
            "observations": conn.execute(
                "SELECT o.*,d.url,d.sha256 FROM latest_observations o JOIN documents d ON d.id=o.dataset_id WHERE o.company_cik=%s ORDER BY o.metric_code,o.period_type",
                (cik,),
            ).fetchall(),
            "source_checks": conn.execute(
                "SELECT * FROM source_checks WHERE company_cik=%s ORDER BY id DESC LIMIT 30",
                (cik,),
            ).fetchall(),
            "documents": conn.execute(
                "SELECT * FROM documents WHERE company_cik=%s ORDER BY id DESC", (cik,)
            ).fetchall(),
            "tasks": conn.execute(
                "SELECT * FROM tasks WHERE company_cik=%s ORDER BY id DESC LIMIT 100",
                (cik,),
            ).fetchall(),
            "extractions": conn.execute(
                "SELECT e.* FROM extractions e JOIN documents d ON d.id=e.document_id WHERE d.company_cik=%s ORDER BY e.id DESC",
                (cik,),
            ).fetchall(),
            "metrics": conn.execute(
                "SELECT * FROM analysis_metrics WHERE company_cik=%s ORDER BY id DESC",
                (cik,),
            ).fetchall(),
        }
