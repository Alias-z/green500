"""Command-line collection and viewer entry points."""

import argparse
import json
import sys

from green500 import db
from green500.config import load_settings
from green500.storage import validate_public_url


def main() -> int:
    """Run one explicit command and return a failure code when work did not succeed."""
    parser = argparse.ArgumentParser(
        description="Source-backed environmental data and one-table Ops"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    commands.add_parser("monitor")
    collect = commands.add_parser("collect")
    collect.add_argument(
        "source",
        choices=[
            "sp500",
            "report",
            "directory",
            "feed",
            "financial",
            "targets",
            "sec-reports",
            "sustainability-reports",
        ],
    )
    collect.add_argument("--limit", type=int)
    collect.add_argument("--cik")
    collect.add_argument("--url")
    collect.add_argument("--year", type=int)
    collect.add_argument("--page-limit", type=int, default=10)
    collect.add_argument("--enqueue", action="store_true")
    execute = commands.add_parser("execute")
    execute.add_argument("task_id", type=int)
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=19973)
    args = parser.parse_args()
    settings = load_settings()
    if args.command == "monitor":
        from green500.monitor import install_default_schedules

        install_default_schedules(settings)
        print(
            json.dumps(
                {"ok": True, "message": "Core and known company sources scheduled."}
            )
        )
        return 0
    if args.command == "init-db":
        db.initialize_database(settings)
        print(json.dumps({"ok": True}))
        return 0
    if args.command == "serve":
        import uvicorn

        if (
            args.host not in {"127.0.0.1", "localhost", "::1"}
            and not settings.ops_token
        ):
            raise ValueError("Set GREEN500_OPS_TOKEN before serving outside loopback.")
        uvicorn.run("green500.web:app", host=args.host, port=args.port)
        return 0
    if args.command == "execute":
        task = db.get_task(settings, args.task_id)
        if task["status"] != "running":
            raise ValueError("Only a claimed running task can execute.")
        try:
            if task["kind"] == "collect_financial":
                from green500.financial_sources import collect_financial_task

                result = collect_financial_task(task)
            elif task["kind"] == "collect_targets":
                from green500.target_sources import collect_targets_task

                result = collect_targets_task(task)
            elif task["kind"] == "collect_sec_reports":
                from green500.sec_reports import collect_sec_reports_task

                result = collect_sec_reports_task(task)
            elif task["kind"] == "collect_sustainability_reports":
                from green500.sustainability_reports import (
                    collect_sustainability_reports_task,
                )

                result = collect_sustainability_reports_task(task)
            elif task["kind"] == "extract":
                from green500.llm import extract_task

                result = extract_task(task)
            else:
                from green500.crawl import collect_task

                result = collect_task(task)
            db.finish(
                settings,
                task["id"],
                "partial" if result.get("is_partial") else "succeeded",
                result=result,
                error="Some content or observations need review."
                if result.get("is_partial")
                else None,
            )
            print(json.dumps(result, default=str))
            return 0
        except Exception as error:  # noqa: BLE001 - task boundary records every outcome
            from green500.llm import ModelOutcomeUnknown

            status = "unknown" if isinstance(error, ModelOutcomeUnknown) else "failed"
            db.finish(
                settings,
                task["id"],
                status,
                error=str(error)[:1000],
            )
            print(json.dumps({"error": str(error)[:1000]}))
            return 1
    from green500.worker import run_one

    if args.command == "worker":
        if args.once:
            print(json.dumps(run_one(settings), default=str))
        else:
            import threading

            from green500.worker import worker_loop

            worker_loop(settings, threading.Event())
        return 0
    task_ids = []
    if args.command == "collect":
        if args.source == "sp500":
            task_ids.append(db.enqueue(settings, "collect_sp500", None, {}))
        elif args.source in {
            "financial",
            "targets",
            "sec-reports",
            "sustainability-reports",
        }:
            value = {"limit": args.limit} if args.limit else {}
            task_ids.append(
                db.enqueue(
                    settings,
                    "collect_" + args.source.replace("-", "_"),
                    args.cik.zfill(10) if args.cik else None,
                    value,
                )
            )
        else:
            if not args.cik or not args.url:
                parser.error("Company collection requires --cik and --url.")
            args.url = validate_public_url(args.url)
            task_ids.append(
                db.enqueue(
                    settings,
                    "collect_source",
                    args.cik.zfill(10),
                    {
                        "url": args.url,
                        "source_kind": args.source,
                        "year": args.year,
                        "page_limit": args.page_limit,
                    },
                )
            )
    if args.enqueue:
        print(json.dumps({"task_ids": task_ids}))
        return 0
    results = [run_one(settings, task_id) for task_id in task_ids]
    print(json.dumps(results, default=str))
    return (
        0 if results and all(r and r["status"] == "succeeded" for r in results) else 1
    )


if __name__ == "__main__":
    sys.exit(main())
