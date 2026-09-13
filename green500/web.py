"""Single-table Ops API and static page, backed by the same durable task records."""

import hmac
import io
import json
import re
import threading
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from green500 import (
    catalog,
    db,
    feature_catalog,
    processed_financial,
    processed_reports,
    view_access,
)
from green500.config import load_settings
from green500.model_comparison import router as model_comparison_router
from green500.storage import read_bytes, save_json, validate_public_url
from green500.worker import worker_loop

STATIC_DIR = Path(__file__).with_name("static")


@asynccontextmanager
async def lifespan(app):
    """Run one local task consumer alongside the Ops server."""
    if not load_settings().run_worker:
        yield
        return
    stop = threading.Event()
    thread = threading.Thread(
        target=worker_loop, args=(load_settings(), stop), daemon=True
    )
    thread.start()
    yield
    stop.set()
    thread.join(timeout=2)


app = FastAPI(title="Green500", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(view_access.router)

app.include_router(model_comparison_router)


@app.get("/portfolio")
def portfolio_page():
    """Serve the comparison page; its data APIs require a viewer session."""
    return FileResponse(STATIC_DIR / "portfolio.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Avoid a failed browser icon request when an original opens in its own tab."""
    return Response(status_code=204)


@app.middleware("http")
async def prevent_private_response_caching(request: Request, call_next):
    """Keep password-protected data out of shared reverse-proxy caches."""
    response = await call_next(request)
    if request.url.path.startswith("/api/") or request.url.path in {"/", "/portfolio"}:
        response.headers["Cache-Control"] = "private, no-store"
    if request.url.path == "/api/catalog" and response.status_code == 200:
        view_access.refresh_session_cookie(response, request)
    return response


def authorize(request: Request, authorization: str = Header(default="")) -> None:
    """Require a token when configured and reject cross-origin mutation attempts."""
    settings = load_settings()
    if settings.ops_token and not hmac.compare_digest(
        authorization, "Bearer " + settings.ops_token
    ):
        raise HTTPException(
            401, "Enter the Ops token from the local environment configuration."
        )
    if request.method not in {"GET", "HEAD"}:
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if origin and origin.rstrip("/") != expected:
            raise HTTPException(403, "Cross-origin writes are not allowed.")


@app.get("/")
def index():
    """Serve the company table without exposing configuration secrets."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/docs/data-design", dependencies=[Depends(view_access.authorize_view)])
def data_design():
    """Publish the English architecture document for the project team."""
    return FileResponse(
        STATIC_DIR.parents[1] / "DATA_DESIGN.md", media_type="text/plain"
    )


@app.get("/docs/data-sources", dependencies=[Depends(view_access.authorize_view)])
def data_sources():
    """Publish the English source-category inventory for the project team."""
    return FileResponse(
        STATIC_DIR.parents[1] / "data/DATA_SOURCES.md", media_type="text/plain"
    )


@app.get("/api/catalog", dependencies=[Depends(view_access.authorize_view)])
def report_catalog():
    """Show all current companies and verified report download coverage."""
    return catalog.company_catalog(load_settings())


@app.get("/api/catalog/{cik}", dependencies=[Depends(view_access.authorize_view)])
def report_catalog_detail(cik: str):
    """Show original reports and datasets for one company without model receipts."""
    if not re.fullmatch(r"[0-9]{10}", cik):
        raise HTTPException(400, "Invalid CIK.")
    return catalog.catalog_detail(load_settings(), cik)


@app.get("/api/catalog/{cik}/financial", dependencies=[Depends(view_access.authorize_view)])
def financial_result(cik: str, download: bool = False):
    """Return validated annual-filing metrics without exposing provider credentials or requests."""
    if not re.fullmatch(r"[0-9]{10}",cik):
        raise HTTPException(400,"Invalid CIK.")
    settings = load_settings()
    with db.connect(settings) as connection:
        company = connection.execute("SELECT 1 FROM companies WHERE cik=%s AND is_current",(cik,)).fetchone()
    if not company:
        raise HTTPException(404,"Company is not in the current index.")
    result = processed_financial.financial_results(settings).get(cik, {"processing":{"status":"not_processed","has_result":False},"data":None,"evidence":{},"source_document_id":None})
    if download:
        if result["data"] is None:
            raise HTTPException(404,"No validated financial result is available.")
        data = result["data"]
        filename = re.sub(r"[^A-Za-z0-9._-]","_",data["company_id"]) + "_" + str(data["fiscal_year"]) + "_financial.json"
        return Response(json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False),media_type="application/json",headers={"Content-Disposition":f'attachment; filename="{filename}"',"X-Content-Type-Options":"nosniff"})
    return result


@app.get("/api/catalog/{cik}/processed/{category}", dependencies=[Depends(view_access.authorize_view)])
def category_result(cik: str, category: str, download: bool = False):
    """Return a team's structured category output and its original-source references."""
    if not re.fullmatch(r"[0-9]{10}", cik) or category not in processed_reports.PROFILES:
        raise HTTPException(400, "Invalid company or report category.")
    settings = load_settings()
    with db.connect(settings) as connection:
        company = connection.execute("SELECT symbols FROM companies WHERE cik=%s AND is_current", (cik,)).fetchone()
    if not company:
        raise HTTPException(404, "Company is not in the current index.")
    result = processed_reports.report_results(settings).get(cik, {}).get(category, {
        "processing": {"status": "not_processed", "has_result": False},
        "data": None, "evidence": {}, "source_document_id": None,
    })
    if download:
        if result["data"] is None:
            raise HTTPException(404, "No extracted result is available.")
        symbol = company["symbols"][0] if company["symbols"] else cik
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", symbol + "_" + category) + ".json"
        return Response(json.dumps(result["data"], indent=2, ensure_ascii=False, allow_nan=False),
                        media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"', "X-Content-Type-Options": "nosniff"})
    return result


@app.get("/api/features", dependencies=[Depends(view_access.authorize_view)])
def fixed_features(format: Literal["csv", "json", "metadata", "schema"] = "csv"):
    """Download fixed predictors or their source metadata for all current companies."""
    result = feature_catalog.feature_catalog(load_settings())
    if format == "csv":
        body = feature_catalog.features_csv(result)
        filename = "green500_features.csv"
        media_type = "text/csv; charset=utf-8"
    else:
        payload = {
            "json": {
                "generated_at": result["generated_at"],
                "rows": result["rows"],
            },
            "metadata": {
                "generated_at": result["generated_at"],
                "companies": result["metadata"],
            },
            "schema": result["schema"],
        }[format]
        body = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
        filename = {
            "json": "green500_features.json",
            "metadata": "green500_metadata.json",
            "schema": "green500_schema.json",
        }[format]
        media_type = "application/json"
    return Response(
        body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/training-data", dependencies=[Depends(view_access.authorize_view)])
def training_data_download():
    """Download one aligned snapshot for EBM, CatBoost and XGBoost experiments."""
    from green500.training_data import export_training_bundle

    settings = load_settings()
    snapshot = export_training_bundle(settings, settings.data_dir / "ml" / "training")
    directory = Path(snapshot["snapshot_dir"])
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("X.csv", "labels.csv", "companies.csv", "metadata.json", "schema.json", "coverage.json", "manifest.json"):
            archive.write(directory / name, arcname=name)
    return Response(
        stream.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="green500_training_' + snapshot["snapshot_id"] + '.zip"',
                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/catalog/files/{document_id}/{action}", dependencies=[Depends(view_access.authorize_view)])
def report_original(document_id: int, action: Literal["view", "download", "text"]):
    """Open original PDF/text or download bytes; acquired HTML never executes."""
    settings = load_settings()
    document = catalog.catalog_document(settings, document_id)
    if not document:
        raise HTTPException(404, "Original file is not in the report catalog.")
    if action == "text" and document.get("preview_version") == "source-text-v1" and document.get("preview_sha256"):
        try:
            return json.loads(read_bytes(settings.data_dir, document["preview_sha256"]))
        except (ValueError, OSError):
            pass
    try:
        body = read_bytes(settings.data_dir, document["sha256"])
    except (ValueError, OSError) as error:
        raise HTTPException(409, "Original file is missing or failed integrity verification.") from error
    content_type = document["content_type"].split(";",1)[0].lower()
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", urlsplit(document["final_url"]).path.rsplit("/",1)[-1])[:150]
    if action == "text":
        from green500.source_text import build_source_text

        try:
            preview = build_source_text(body,document["content_type"],filename)
        except (ValueError, OSError) as error:
            raise HTTPException(422,str(error)[:500]) from error
        preview.update(source_sha256=document["sha256"],source_url=document["url"],parser_version="source-text-v1")
        digest=save_json(settings.data_dir,preview)
        with db.connect(settings) as conn:
            conn.execute("UPDATE documents SET preview_sha256=%s,preview_version='source-text-v1' WHERE id=%s",(digest,document_id))
        return preview
    disposition="attachment"
    media_type="application/octet-stream"
    if action=="view":
        if body.startswith(b"%PDF-"):
            media_type="application/pdf"
            disposition="inline"
        elif content_type in {"text/html","text/plain","text/xml","application/xml","application/json","text/csv"}:
            media_type="application/json" if content_type=="application/json" else "text/plain"
            disposition="inline"
    return Response(body,media_type=media_type,headers={
        "Content-Disposition":f'{disposition}; filename="{filename or str(document_id)+".bin"}"',
        "X-Content-Type-Options":"nosniff",
        "Content-Security-Policy":"sandbox; default-src 'none'; frame-ancestors 'self'",
        "Referrer-Policy":"no-referrer",
    })


@app.get("/api/schedules", dependencies=[Depends(authorize)])
def schedules():
    """Expose planned source checks and their most recently queued tasks."""
    with db.connect(load_settings()) as conn:
        return conn.execute("SELECT * FROM source_schedules ORDER BY id").fetchall()


@app.get("/api/observations", dependencies=[Depends(authorize)])
def observations():
    """Deliver latest structured observations and exact source references for analysis."""
    with db.connect(load_settings()) as conn:
        return conn.execute(
            "SELECT o.*,d.url,d.sha256 FROM latest_observations o JOIN documents d ON d.id=o.dataset_id JOIN companies c ON c.cik=o.company_cik WHERE c.is_current ORDER BY o.company_cik,o.metric_code,o.period_type"
        ).fetchall()


@app.get("/api/companies", dependencies=[Depends(authorize)])
def companies(search: str = "", year: int | None = None):
    """Return all matching company rows and the current source's provenance."""
    settings = load_settings()
    with db.connect(settings) as conn:
        control = conn.execute("SELECT * FROM controls WHERE id=1").fetchone()
        source = conn.execute(
            "SELECT id,url,fetched_at,source_updated_at,revision FROM documents WHERE id=%s",
            (control["current_index_document_id"],),
        ).fetchone()
        counts = conn.execute(
            "SELECT status,count(*) AS count FROM tasks GROUP BY status"
        ).fetchall()
    return {
        "companies": db.company_rows(settings, search, year),
        "control": control,
        "source": source,
        "task_counts": counts,
        "model": settings.llm_model,
        "has_model_key": bool(settings.llm_api_key),
    }


@app.get("/api/companies/{cik}", dependencies=[Depends(authorize)])
def detail(cik: str):
    """Show a company's documents, extractions, evidence and retained task errors."""
    if len(cik) != 10 or not cik.isdigit():
        raise HTTPException(400, "Invalid CIK.")
    return db.company_detail(load_settings(), cik)


@app.get("/api/tasks", dependencies=[Depends(authorize)])
def recent_tasks():
    """Expose recent index and company work, including partial collection results."""
    with db.connect(load_settings()) as conn:
        return conn.execute(
            "SELECT id,kind,company_cik,status,created_at,started_at,finished_at,error,result FROM tasks ORDER BY id DESC LIMIT 100"
        ).fetchall()


class Action(BaseModel):
    """Only expose the bounded actions needed by the single Ops table."""

    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "collect_sp500",
        "collect_source",
        "collect_financial",
        "collect_targets",
    ]
    company_cik: str | None = Field(default=None, pattern=r"^\d{10}$")
    url: str | None = Field(default=None, max_length=3000)
    source_kind: Literal["report", "directory", "feed"] = "report"
    year: int | None = Field(default=None, ge=1900, le=2200)
    document_id: int | None = Field(default=None, gt=0)
    force: bool = False
    page_limit: int = Field(default=10, ge=1, le=25)


@app.post("/api/actions", dependencies=[Depends(authorize)])
def create_action(action: Action):
    """Persist requested work; the HTTP request does not run a crawl or model call."""
    settings = load_settings()
    if action.action == "collect_sp500":
        return {"task_id": db.enqueue(settings, "collect_sp500", None, {})}
    if action.action in {"collect_financial", "collect_targets"}:
        return {"task_id": db.enqueue(settings, action.action, action.company_cik, {})}
    if not action.company_cik:
        raise HTTPException(400, "Select a company.")
    with db.connect(settings) as conn:
        company = conn.execute(
            "SELECT cik FROM companies WHERE cik=%s", (action.company_cik,)
        ).fetchone()
    if not company:
        raise HTTPException(404, "Company does not exist.")
    if action.action == "collect_source":
        try:
            action.url = validate_public_url(action.url or "")
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error
        value = {
            "url": action.url,
            "source_kind": action.source_kind,
            "year": action.year,
            "page_limit": action.page_limit,
        }
    else:
        with db.connect(settings) as conn:
            document = conn.execute(
                "SELECT id FROM documents WHERE id=%s AND company_cik=%s AND kind IN ('report','feed')",
                (action.document_id, action.company_cik),
            ).fetchone()
        if not document:
            raise HTTPException(400, "Select a report belonging to this company.")
        value = {"document_id": action.document_id, "force": action.force}
    return {"task_id": db.enqueue(settings, action.action, action.company_cik, value)}


@app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(authorize)])
def retry_task(task_id: int):
    """Create an explicit retry while retaining the previous attempt and output."""
    settings = load_settings()
    try:
        task = db.get_task(settings, task_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    if task["status"] not in {"failed", "unknown", "cancelled", "partial"}:
        raise HTTPException(
            409, "Only failed, unknown or cancelled tasks can be retried."
        )
    return {
        "task_id": db.enqueue(
            settings, task["kind"], task["company_cik"], task["input"]
        )
    }


class Pause(BaseModel):
    """Pause new work without killing an in-flight database or model operation."""

    is_paused: bool


@app.post("/api/pause", dependencies=[Depends(authorize)])
def pause(value: Pause):
    """Persist the task admission setting for both CLI and web workers."""
    with db.connect(load_settings()) as conn:
        conn.execute("UPDATE controls SET is_paused=%s WHERE id=1", (value.is_paused,))
    return value


@app.get("/api/objects/{digest}", dependencies=[Depends(authorize)])
def source_object(digest: str):
    """Expose recorded evidence as a download; never execute acquired HTML on the Ops origin."""
    settings = load_settings()
    with db.connect(settings) as conn:
        known = conn.execute(
            """SELECT 1 FROM documents WHERE sha256=%s OR parsed_sha256=%s
            UNION ALL SELECT 1 FROM extractions WHERE input_sha256=%s OR response_sha256=%s OR result_sha256=%s LIMIT 1""",
            (digest,) * 5,
        ).fetchone()
    if not known:
        raise HTTPException(404, "Source artifact is not registered.")
    try:
        body = read_bytes(settings.data_dir, digest)
    except (ValueError, OSError) as error:
        raise HTTPException(
            409, "Source artifact is missing or failed integrity verification."
        ) from error
    return Response(
        body,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{digest}.bin"',
            "X-Content-Type-Options": "nosniff",
        },
    )
