"""Bounded parallel collection with durable subprocess outcomes."""

import logging
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from green500 import db
from green500.config import PROJECT_DIR
from green500.monitor import enqueue_due_sources
from green500.storage import save_bytes

_LOGGER = logging.getLogger(__name__)


def run_one(settings, task_id: int | None = None) -> dict | None:
    """Claim and execute a task without keeping a transaction open during external work."""
    task = db.claim(settings, task_id)
    if not task:
        return None
    try:
        result = subprocess.run(
            [sys.executable, "-m", "green500", "execute", str(task["id"])],
            cwd=PROJECT_DIR,
            capture_output=True,
            timeout=7200
            if task["kind"] in {"collect_sec_reports", "collect_sustainability_reports"}
            else settings.task_timeout_seconds,
            check=False,
        )
        log_digest = save_bytes(settings.data_dir, result.stdout + result.stderr)
        with db.connect(settings) as conn:
            conn.execute(
                "UPDATE task_attempts SET detail=detail || %s::jsonb WHERE id=%s",
                ('{"log_sha256":"' + log_digest + '"}', task["attempt_id"]),
            )
        current = db.get_task(settings, task["id"])
        if current["status"] == "running":
            db.finish(
                settings,
                task["id"],
                "failed",
                error="Task process exited without a completion receipt; inspect its log.",
            )
    except subprocess.TimeoutExpired as error:
        log_digest = save_bytes(
            settings.data_dir, (error.stdout or b"") + (error.stderr or b"")
        )
        db.finish(
            settings,
            task["id"],
            "failed",
            result={"log_sha256": log_digest},
            error="Task exceeded its configured execution limit.",
        )
    return db.get_task(settings, task["id"])


def worker_loop(settings, stop: threading.Event) -> None:
    """Run up to two queued collection tasks concurrently."""
    active = {}
    with ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="green500-collection"
    ) as executor:
        while not stop.is_set():
            try:
                for task_id, (future, kind) in list(active.items()):
                    if future.done():
                        del active[task_id]
                        future.result()
                enqueue_due_sources(settings)
                while len(active) < 2:
                    with db.connect(settings) as conn:
                        if conn.execute(
                            "SELECT is_paused FROM controls WHERE id=1"
                        ).fetchone()["is_paused"]:
                            break
                        task = conn.execute(
                            "SELECT id,kind FROM tasks WHERE status='pending' AND NOT (id=ANY(%s::bigint[])) ORDER BY id LIMIT 1",
                            (list(active),),
                        ).fetchone()
                    if not task:
                        break
                    active[task["id"]] = (
                        executor.submit(run_one, settings, task["id"]),
                        task["kind"],
                    )
            except Exception:
                _LOGGER.exception(
                    "Green500 task worker could not read or execute pending work"
                )
            stop.wait(1)
