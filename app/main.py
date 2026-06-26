from __future__ import annotations

import json
import shutil
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_session, init_database
from app.models import AnalysisJob, Appeal, ChatMessage, ChatSession, ImportBatch, Region, Report
from app.services.agent import ask_question, prepare_ask_context
from app.services.deepseek import DeepSeekService
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


def _sse(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/ask/stream")
def ask_stream(
    question: str = Form(...),
    city: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
) -> StreamingResponse:
    def source_to_dict(source) -> dict[str, object]:
        return {
            "rank": source.rank,
            "title": source.title,
            "received_at": source.received_at,
            "topic": source.topic,
            "department": source.department,
            "external_id": source.external_id,
            "matched_fields": list(source.matched_fields),
            "content_excerpt": source.content_excerpt,
            "reply_excerpt": source.reply_excerpt,
        }

    def stream():
        answer = ""
        answer_source = "local-statistics + rag"
        with SessionLocal() as stream_session:
            yield _sse(
                "status",
                {"title": "思考中", "detail": "正在检索数据"},
            )
            context = prepare_ask_context(
                stream_session,
                question,
                city=city or None,
                start=start or None,
                end=end or None,
            )
            llm = DeepSeekService()
            if not llm.enabled:
                answer = (
                    "当前未配置 DeepSeek API Key，先返回数据库统计结果。\n\n"
                    + context.facts
                    + "\n\n"
                    + context.evidence_summary
                    + (
                        "\n\n代表性案例：\n" + context.evidence.evidence_text
                        if context.evidence.evidence_text
                        else "\n\n未检索到足够相关的代表性案例。"
                    )
                    + "\n\n说明：当前主题排行来自导入时生成的规则初标；配置 API Key 后可生成更完整的研判答复。"
                )
                yield _sse("delta", {"text": answer})
            else:
                answer_source = f"{llm.model_name} + rag"
                yield _sse(
                    "status",
                    {"title": "思考中", "detail": "模型正在生成回答"},
                )
                try:
                    pieces: list[str] = []
                    for piece in llm.stream_complete(context.system_prompt, context.user_prompt):
                        pieces.append(piece)
                        yield _sse("delta", {"text": piece})
                    answer = "".join(pieces)
                except Exception:
                    answer_source = "local-statistics + rag (AI fallback)"
                    answer = (
                        "DeepSeek 当前调用失败，已回退为数据库统计结果。\n\n"
                        + context.facts
                        + "\n\n"
                        + context.evidence_summary
                        + (
                            "\n\n代表性案例：\n" + context.evidence.evidence_text
                            if context.evidence.evidence_text
                            else "\n\n未检索到足够相关的代表性案例。"
                        )
                        + "\n\n说明：可检查 API Key、模型名称或网络连接后重试。"
                    )
                    yield _sse("reset", {"text": answer})

            chat = ChatSession(title=question[:80])
            chat.messages.extend(
                [
                    ChatMessage(role="user", content=question),
                    ChatMessage(role="assistant", content=answer),
                ]
            )
            stream_session.add(chat)
            stream_session.commit()
            yield _sse(
                "done",
                {
                    "answer_source": answer_source,
                    "answer_html": render_markdown(answer),
                    "rag_evidence": {
                        "candidate_count": context.evidence.candidate_count,
                        "embedding_candidate_count": context.evidence.embedding_candidate_count,
                        "relevant_count": context.evidence.relevant_count,
                        "selected_sources": [
                            source_to_dict(source) for source in context.evidence.selected_sources
                        ],
                    },
                },
            )

    return StreamingResponse(stream(), media_type="text/event-stream")


CITY_COORDINATES = {
    "苏州市": {"lng": 120.5853, "lat": 31.2989},
}


def _report_chart_payload(report: Report | None, session: Session) -> dict[str, object]:
    if not report:
        return {"topics": [], "top_topics": [], "types": [], "meta": {}}
    region = session.get(Region, report.region_id)
    if not region:
        return {"topics": [], "top_topics": [], "types": [], "meta": {}}
    start = report.period_start.date().isoformat() if report.period_start else None
    end = report.period_end.date().isoformat() if report.period_end else None
    stats = dashboard_stats(session, province=region.province, city=region.city, start=start, end=end)
    return {
        "topics": stats["topics"][:8],
        "top_topics": (stats.get("subtopics") or stats["topics"])[:8],
        "types": stats["types"][:6],
        "meta": {
            "city": region.city,
            "province": region.province,
            "total": stats["total"],
            "responded": stats["responded"],
            "response_rate": stats["response_rate"],
            "average_response_hours": stats["average_response_hours"],
        },
    }


@app.get("/map", response_class=HTMLResponse)
def map_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    context = _base_context(request, session)
    return templates.TemplateResponse(request=request, name="map.html", context=context)


@app.get("/api/map/overview")
def map_overview(session: Session = Depends(get_session)) -> dict[str, object]:
    cities: list[dict[str, object]] = []
    regions = available_regions(session)
    for region in regions:
        coordinates = CITY_COORDINATES.get(region.city) or (
            {"lng": 120.5853, "lat": 31.2989}
            if "苏州" in region.city or len(regions) == 1
            else None
        )
        if not coordinates:
            continue
        stats = dashboard_stats(session, province=region.province, city=region.city)
        cities.append(
            {
                "province": region.province,
                "city": region.city,
                "lng": coordinates["lng"],
                "lat": coordinates["lat"],
                "total": stats["total"],
                "responded": stats["responded"],
                "response_rate": stats["response_rate"],
                "average_response_hours": stats["average_response_hours"],
                "top_topics": stats["topics"][:5],
                "report_url": f"/reports?city={quote(region.city)}",
            }
        )
    return {"cities": cities}


@app.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    selected: int | None = None,
    city: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    selected_region = session.scalar(select(Region).where(Region.city == city)) if city else None
    reports_statement = select(Report).order_by(Report.created_at.desc()).limit(30)
    if selected_region:
        reports_statement = (
            select(Report)
            .where(Report.region_id == selected_region.id)
            .order_by(Report.created_at.desc())
            .limit(30)
        )
    reports = list(session.scalars(reports_statement).all())
    report = session.get(Report, selected) if selected else (reports[0] if reports else None)
    report_html = render_markdown(report.content) if report else ""
    report_chart_json = (
        json.dumps(_report_chart_payload(report, session), ensure_ascii=False, default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    context = _base_context(request, session)
    context.update(
        {
            "reports": reports,
            "report": report,
            "report_html": report_html,
            "report_chart_json": report_chart_json,
            "selected_city": city or "",
        }
    )
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
