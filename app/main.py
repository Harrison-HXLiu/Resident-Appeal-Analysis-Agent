from __future__ import annotations

import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_session, init_database
from app.models import AnalysisJob, Appeal, ChatMessage, ChatSession, ImportBatch, Region, Report
from app.services.agent import ask_question
from app.services.ai_annotation import refine_annotations_with_ai
from app.services.analytics import available_regions, clear_dashboard_cache, dashboard_stats
from app.services.importer import backfill_rule_annotations, import_excel
from app.services.markdown import render_markdown
from app.services.rag import backfill_chunks, ensure_fts_index, rebuild_fts_index
from app.services.reports import create_report


settings = get_settings()
templates = Jinja2Templates(directory=str(settings.base_dir / "app" / "templates"))


def _import_sample_if_empty() -> None:
    if not settings.auto_import_sample:
        return
    with SessionLocal() as session:
        if session.scalar(select(func.count(Appeal.id))) == 0:
            samples = sorted(settings.data_dir.glob("*.xlsx"))
            if samples:
                import_excel(session, samples[0], settings.default_province, settings.default_city)
        else:
            backfill_rule_annotations(session)
        backfill_chunks(session)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    ensure_fts_index()
    _import_sample_if_empty()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(settings.base_dir / "app" / "static")), name="static")


def _base_context(request: Request, session: Session) -> dict[str, object]:
    return {
        "request": request,
        "app_name": settings.app_name,
        "regions": available_regions(session),
        "llm_enabled": bool(settings.deepseek_api_key.strip()),
        "model_name": settings.deepseek_model,
        "embedding_enabled": bool(settings.dashscope_api_key.strip()),
        "embedding_model": settings.embedding_model,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(
        settings.base_dir / "app" / "static" / "favicon.svg",
        media_type="image/svg+xml",
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    city: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    selected_city = city or settings.default_city
    stats = dashboard_stats(session, city=selected_city, start=start, end=end)
    stats_json = (
        json.dumps(stats, ensure_ascii=False, default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    context = _base_context(request, session)
    context.update(
        {
            "stats": stats,
            "stats_json": stats_json,
            "selected_city": selected_city,
            "start": start or "",
            "end": end or "",
        }
    )
    return templates.TemplateResponse(request=request, name="dashboard.html", context=context)


@app.get("/api/dashboard")
def dashboard_api(
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return dashboard_stats(session, city=city, start=start, end=end)


@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    context = _base_context(request, session)
    context.update(
        {
            "question": "",
            "answer": None,
            "answer_html": None,
            "answer_source": None,
            "rag_evidence": None,
            "selected_city": settings.default_city,
        }
    )
    return templates.TemplateResponse(request=request, name="ask.html", context=context)


@app.post("/ask", response_class=HTMLResponse)
def ask_submit(
    request: Request,
    question: str = Form(...),
    city: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    answer, answer_source, rag_evidence = ask_question(
        session, question, city=city or None, start=start or None, end=end or None
    )
    chat = ChatSession(title=question[:80])
    chat.messages.extend(
        [ChatMessage(role="user", content=question), ChatMessage(role="assistant", content=answer)]
    )
    session.add(chat)
    session.commit()
    context = _base_context(request, session)
    context.update(
        {
            "question": question,
            "answer": answer,
            "answer_html": render_markdown(answer),
            "answer_source": answer_source,
            "rag_evidence": rag_evidence,
            "selected_city": city,
            "start": start,
            "end": end,
        }
    )
    return templates.TemplateResponse(request=request, name="ask.html", context=context)


@app.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    selected: int | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    reports = list(session.scalars(select(Report).order_by(Report.created_at.desc()).limit(30)).all())
    report = session.get(Report, selected) if selected else (reports[0] if reports else None)
    context = _base_context(request, session)
    context.update({"reports": reports, "report": report})
    return templates.TemplateResponse(request=request, name="reports.html", context=context)


@app.post("/reports", response_class=HTMLResponse)
def reports_generate(
    region_id: int = Form(...),
    start: str = Form(""),
    end: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    region = session.get(Region, region_id)
    if not region:
        raise HTTPException(status_code=404, detail="地区不存在")
    report = create_report(session, region, start or None, end or None)
    return RedirectResponse(url=f"/reports?selected={report.id}", status_code=303)


@app.get("/reports/{report_id}/download")
def report_download(report_id: int, session: Session = Depends(get_session)) -> PlainTextResponse:
    report = session.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    filename = quote(f"{report.title}.md")
    return PlainTextResponse(
        report.content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@app.get("/data", response_class=HTMLResponse)
def data_page(
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    batches = list(
        session.scalars(select(ImportBatch).order_by(ImportBatch.imported_at.desc()).limit(30)).all()
    )
    jobs = list(session.scalars(select(AnalysisJob).order_by(AnalysisJob.created_at.desc()).limit(10)).all())
    context = _base_context(request, session)
    context.update({"batches": batches, "jobs": jobs, "message": message})
    return templates.TemplateResponse(request=request, name="data.html", context=context)


@app.post("/data/import")
def upload_data(
    file: UploadFile = File(...),
    province: str = Form(...),
    city: str = Form(...),
    district: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".xlsx":
        raise HTTPException(status_code=400, detail="当前仅支持 .xlsx 文件")
    destination = settings.uploads_dir / f"{uuid.uuid4().hex}.xlsx"
    with destination.open("wb") as target:
        shutil.copyfileobj(file.file, target)
    try:
        result = import_excel(session, destination, province, city, district)
    except ValueError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    clear_dashboard_cache()
    backfill_chunks(session)
    rebuild_fts_index()
    message = (
        "该文件此前已导入，未重复写入。"
        if result.skipped
        else f"导入完成：新增 {result.inserted} 条，更新 {result.updated} 条。"
    )
    return RedirectResponse(url=f"/data?message={quote(message)}", status_code=303)


@app.post("/data/annotate")
def run_ai_annotation(
    city: str = Form(""),
    limit: int = Form(20),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    job = refine_annotations_with_ai(session, city or None, limit)
    clear_dashboard_cache()
    message = (
        f"AI 复核完成：处理 {job.processed_count} 条，失败 {job.failed_count} 条。"
        if job.status == "completed"
        else f"AI 复核未完成：{job.message or '请检查 API 配置。'}"
    )
    return RedirectResponse(url=f"/data?message={quote(message)}", status_code=303)
