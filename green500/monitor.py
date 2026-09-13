"""Schedule due source checks using the existing durable task queue."""

from psycopg.types.json import Jsonb

from green500 import db


def register_schedule(settings, name, task_kind, company_cik, value, interval_hours):
    """Register a recurring check without resetting an existing schedule deadline."""
    with db.connect(settings) as conn:
        conn.execute(
            """INSERT INTO source_schedules(name,task_kind,company_cik,input,interval_hours,next_check_at)
            VALUES (%s,%s,%s,%s,%s,now()+%s*interval '1 hour')
            ON CONFLICT(name) DO UPDATE SET task_kind=EXCLUDED.task_kind,input=EXCLUDED.input,
            interval_hours=EXCLUDED.interval_hours""",
            (
                name,
                task_kind,
                company_cik,
                Jsonb(value),
                interval_hours,
                interval_hours,
            ),
        )


def install_default_schedules(settings):
    """Monitor core datasets and known company reports without dispatching model calls."""
    for name, kind, hours in [
        ("S&P 500 constituents", "collect_sp500", 24),
        ("SEC current financial facts", "collect_financial", 24),
        ("SBTi current targets", "collect_targets", 168),
    ]:
        register_schedule(settings, name, kind, None, {}, hours)
    with db.connect(settings) as conn:
        documents = conn.execute(
            "SELECT DISTINCT ON (company_cik,url,kind) company_cik,url,kind,reporting_year FROM documents WHERE kind IN ('report','directory','feed') AND company_cik IS NOT NULL ORDER BY company_cik,url,kind,id DESC"
        ).fetchall()
    for document in documents:
        register_schedule(
            settings,
            document["company_cik"] + " " + document["url"],
            "collect_source",
            document["company_cik"],
            {
                "url": document["url"],
                "source_kind": document["kind"],
                "year": document["reporting_year"],
                "page_limit": 10,
            },
            24 if document["kind"] == "feed" else 168,
        )


def enqueue_due_sources(settings):
    """Queue each enabled due source once and preserve its refresh schedule."""
    with db.connect(settings) as conn:
        if conn.execute("SELECT is_paused FROM controls WHERE id=1").fetchone()[
            "is_paused"
        ]:
            return []
        # Serialize schedule admission across CLI and web consumers.
        if not conn.execute(
            "SELECT pg_try_advisory_xact_lock(5002027) AS acquired"
        ).fetchone()["acquired"]:
            return []
        schedules = conn.execute(
            """SELECT s.* FROM source_schedules s WHERE s.is_enabled AND s.next_check_at<=now()
            AND NOT EXISTS(SELECT 1 FROM tasks t WHERE t.id=s.last_task_id AND t.status IN ('pending','running'))
            ORDER BY s.id FOR UPDATE"""
        ).fetchall()
        task_ids = []
        for schedule in schedules:
            task_id = db.enqueue(
                settings,
                schedule["task_kind"],
                schedule["company_cik"],
                schedule["input"],
            )
            conn.execute(
                "UPDATE source_schedules SET last_task_id=%s,next_check_at=now()+interval_hours*interval '1 hour' WHERE id=%s",
                (task_id, schedule["id"]),
            )
            task_ids.append(task_id)
    return task_ids
