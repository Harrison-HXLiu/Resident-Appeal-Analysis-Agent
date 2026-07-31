from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import AnalysisJob


JobHandler = Callable[[Session, AnalysisJob, dict[str, object]], Optional[dict[str, object]]]

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis-worker")
_futures: dict[str, Future[None]] = {}
_lock = Lock()


def _run_import(session: Session, job: AnalysisJob, payload: dict[str, object]):
    from app.services.importer import import_excel

    job.progress = 5
    session.commit()
    source_path = Path(str(payload["path"])).resolve()
    result = import_excel(
        session,
        source_path,
        str(payload["province"]),
        str(payload["city"]),
        str(payload.get("district") or ""),
        source_platform_code=str(payload.get("source_platform_code") or ""),
        source_platform_name=str(payload.get("source_platform_name") or ""),
        source_url=str(payload.get("source_url") or ""),
        province_code=str(payload.get("province_code") or ""),
        city_code=str(payload.get("city_code") or ""),
        district_code=str(payload.get("district_code") or ""),
    )
    uploads_dir = get_settings().uploads_dir.resolve()
    if uploads_dir in source_path.parents:
        source_path.unlink(missing_ok=True)
    job.processed_count = result.rows
    job.failed_count = result.failed
    return {
        "batch_id": result.batch_id,
        "rows": result.rows,
        "inserted": result.inserted,
        "updated": result.updated,
        "failed": result.failed,
        "skipped": result.skipped,
    }


def _run_snapshot(session: Session, job: AnalysisJob, payload: dict[str, object]):
    from app.services.report_documents import pregenerate_standard_reports
    from app.services.snapshots import build_quarter_snapshot

    snapshot = build_quarter_snapshot(session, str(payload["quarter"]), job=job)
    reports = {"created": 0, "skipped": 0, "total": 0}
    if get_settings().pregenerate_standard_reports:
        reports = pregenerate_standard_reports(session, snapshot, job=job)
    return {
        "snapshot_id": snapshot.id,
        "quarter": snapshot.quarter,
        "version": snapshot.version,
        "manifest": snapshot.manifest,
        "standard_reports": reports,
    }


def _run_report(session: Session, job: AnalysisJob, payload: dict[str, object]):
    from app.services.report_documents import create_report_document

    report = create_report_document(session, payload, job=job)
    return {"report_id": report.public_id, "status": report.status}


_HANDLERS: dict[str, JobHandler] = {
    "import": _run_import,
    "snapshot": _run_snapshot,
    "report": _run_report,
}


def create_job(session: Session, job_type: str, payload: dict[str, object]) -> AnalysisJob:
    if job_type not in _HANDLERS:
        raise ValueError(f"不支持任务类型：{job_type}")
    job = AnalysisJob(job_type=job_type, status="pending", payload=payload)
    session.add(job)
    session.commit()
    session.refresh(job)
    if get_settings().worker_enabled:
        submit_job(job.public_id)
    return job


def _execute_job(public_id: str) -> None:
    with SessionLocal() as session:
        job = session.scalar(select(AnalysisJob).where(AnalysisJob.public_id == public_id))
        if not job or job.status not in {"pending", "retrying"}:
            return
        job.status = "running"
        job.started_at = datetime.now()
        job.progress = max(job.progress, 1)
        session.commit()
        try:
            result = _HANDLERS[job.job_type](session, job, dict(job.payload or {})) or {}
            job.status = "completed"
            job.progress = 100
            job.result = result
            job.message = ""
            job.finished_at = datetime.now()
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.scalar(select(AnalysisJob).where(AnalysisJob.public_id == public_id))
            if job:
                job.status = "failed"
                job.message = str(exc)[:2000]
                job.finished_at = datetime.now()
                session.commit()
            raise
        finally:
            with _lock:
                _futures.pop(public_id, None)


def submit_job(public_id: str) -> None:
    with _lock:
        if public_id in _futures:
            return
        _futures[public_id] = _executor.submit(_execute_job, public_id)


def recover_pending_jobs() -> int:
    recovered = 0
    with SessionLocal() as session:
        jobs = session.scalars(
            select(AnalysisJob).where(AnalysisJob.status.in_(["pending", "running"]))
        ).all()
        for job in jobs:
            if job.status == "running":
                job.status = "retrying"
                job.message = "应用重启后重新执行"
            submit_job(job.public_id)
            recovered += 1
        session.commit()
    return recovered


def shutdown_worker() -> None:
    _executor.shutdown(wait=False, cancel_futures=False)
