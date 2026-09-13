"""Publish batch report downloads with per-source progress and retained originals."""

from green500 import db
from green500.catalog import register_report_source
from green500.data_store import record_check
from green500.storage import save_bytes


def register_pending_report(settings, task_id, company_cik, source):
    """Expose a discovered source before its queued network request finishes."""
    profile = register_report_source(settings, company_cik, source)
    with db.connect(settings) as conn:
        db.require_active_task(conn, task_id)
        conn.execute("UPDATE report_sources SET download_status='pending',latest_download_task_id=%s,last_download_attempt_at=now() WHERE id=%s",(task_id,profile["id"]))
    return profile["id"]


def save_report(settings, task_id, company_cik, source, body, content_type, final_url):
    """Save verified acquired bytes without extracting metrics or calling a model."""
    if not body or (settings.max_document_bytes and len(body)>settings.max_document_bytes):
        raise ValueError("Report download is empty or exceeds the configured byte limit.")
    profile = register_report_source(settings, company_cik, source)
    digest = save_bytes(settings.data_dir, body)
    with db.connect(settings) as conn:
        db.require_active_task(conn, task_id)
        document_id=db.record_document(conn,company_cik=company_cik,url=profile["url"],final_url=final_url,
            title=source["title"],kind="report",digest=digest,byte_count=len(body),content_type=content_type)
        conn.execute("UPDATE report_sources SET download_status='available',latest_download_task_id=%s,last_download_attempt_at=now() WHERE id=%s",(task_id,profile["id"]))
    record_check(settings,"company_reports",company_cik,"succeeded",dataset_id=document_id,details={"url":profile["url"],"task_id":task_id,"acquisition_method":"scrapy"})
    return document_id


def record_report_failure(settings, task_id, company_cik, url, error):
    """Record a failed discovery/download while retaining any earlier original."""
    with db.connect(settings) as conn:
        db.require_active_task(conn, task_id)
        conn.execute("UPDATE report_sources SET download_status='failed',latest_download_task_id=%s,last_download_attempt_at=now() WHERE company_cik=%s AND url=%s",(task_id,company_cik,url))
    record_check(settings,"company_reports",company_cik,"failed",error=str(error)[:1000],details={"url":url,"task_id":task_id})
