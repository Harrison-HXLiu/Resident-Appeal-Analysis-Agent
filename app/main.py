from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from urllib.parse import quote, urlparse

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_session, init_database
from app.models import (
    AnalysisJob,
    Appeal,
    GoldSample,
    ImportBatch,
    PolicyDocument,
    QuarterSnapshot,
    Region,
    ReportDocument,
    TaxonomyLabel,
    TaxonomyVersion,
    User,
)
from app.schemas import (
    ChatMessageRequest,
    ChatSessionCreateRequest,
    GoldAnnotationRequest,
    GoldSampleCreateRequest,
    ImportBatchRequest,
    PolicyCreateRequest,
    RegionMetadataUpdate,
    ReportCreateRequest,
    ReportUpdateRequest,
    SnapshotRequest,
    TaxonomyLabelUpdate,
    UserCreateRequest,
)
from app.services.analytics import (
    available_quarters,
    available_regions,
    clear_dashboard_cache,
    dashboard_stats,
    map_overview,
)
from app.services.auth import (
    COOKIE_NAME,
    audit,
    authenticate,
    create_user_session,
    ensure_bootstrap_admin,
    has_role,
    hash_password,
    revoke_token,
    user_from_token,
)
from app.services.chat import (
    cleanup_expired_sessions,
    create_chat_session,
    delete_chat_session,
    delete_user_chat_sessions,
    get_active_chat_session,
    prepare_chat_turn,
    save_chat_turn,
)
from app.services.classification import TAXONOMY_RULES
from app.services.gold_samples import (
    arbitrate_gold_sample,
    create_gold_samples,
    get_gold_sample,
    list_gold_samples,
    recompute_taxonomy_metrics,
    submit_gold_annotation,
)
from app.services.importer import backfill_rule_annotations, import_excel
from app.services.jobs import create_job, recover_pending_jobs
from app.services.markdown import render_markdown
from app.services.policy_ingest import MAX_POLICY_BYTES, extract_policy_file, fetch_policy_url
from app.services.providers import active_profile
from app.services.rag import backfill_chunks, ensure_fts_index
from app.services.report_documents import (
    create_policy,
    export_report_docx,
    export_report_pdf,
    publish_report,
    update_report,
)
from app.services.taxonomy import can_publish, ensure_draft_taxonomy, publish_taxonomy


settings = get_settings()
templates = Jinja2Templates(directory=str(settings.base_dir / "app" / "templates"))
_login_attempts: dict[str, deque[float]] = defaultdict(deque)


def _import_sample_if_empty() -> None:
    if not settings.auto_import_sample:
        return
    with SessionLocal() as session:
        if session.scalar(select(func.count(Appeal.id))) == 0:
            samples = sorted(settings.data_dir.glob("*.xlsx"))
            if samples:
                import_excel(
                    session,
                    samples[0],
                    settings.default_province,
                    settings.default_city,
                    source_platform_code="sample-suzhou",
                    source_platform_name="苏州示例数据",
                )
        else:
            backfill_rule_annotations(session)
        if settings.database_url.startswith("sqlite"):
            backfill_chunks(session)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    if settings.database_url.startswith("sqlite"):
        ensure_fts_index()
    with SessionLocal() as session:
        ensure_draft_taxonomy(session)
        ensure_bootstrap_admin(session)
        cleanup_expired_sessions(session)
    _import_sample_if_empty()
    if settings.worker_enabled:
        recover_pending_jobs()
    yield


app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    description="全国地级市居民留言地图、季度报告与可追溯分析对话",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(settings.base_dir / "app" / "static")), name="static")


def _is_public_path(path: str) -> bool:
    return path in {"/health", "/login", "/favicon.ico"} or path.startswith("/static/")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    request.state.user = None
    if settings.auth_required and not _is_public_path(request.url.path):
        with SessionLocal() as session:
            user = user_from_token(session, request.cookies.get(COOKIE_NAME))
        if user is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "请先登录"}, status_code=401)
            next_path = quote(request.url.path)
            return RedirectResponse(f"/login?next={next_path}", status_code=303)
        request.state.user = user

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.url.netloc:
                return JSONResponse({"detail": "跨站请求已拒绝"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'",
    )
    return response


def _require_role(request: Request, role: str) -> User | None:
    user = getattr(request.state, "user", None)
    if not has_role(user, role):
        raise HTTPException(status_code=403, detail="权限不足")
    return user


def _base_context(request: Request, session: Session) -> dict[str, object]:
    taxonomy = session.scalar(
        select(TaxonomyVersion)
        .where(TaxonomyVersion.status.in_(["published", "trial"]))
        .order_by(TaxonomyVersion.published_at.desc().nullslast(), TaxonomyVersion.id.desc())
    )
    profile = active_profile()
    return {
        "request": request,
        "app_name": settings.app_name,
        "regions": available_regions(session),
        "quarters": available_quarters(session),
        "topics": list(TAXONOMY_RULES) + ["其他/综合"],
        "llm_enabled": bool(settings.model_api_key.strip()),
        "model_name": settings.chat_model,
        "model_profile": profile,
        "taxonomy": taxonomy,
        "taxonomy_trial": not taxonomy or taxonomy.status != "published",
        "current_user": getattr(request.state, "user", None),
        "auth_required": settings.auth_required,
        "session_timeout_minutes": settings.session_timeout_minutes,
    }


def _sse(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def _job_dict(job: AnalysisJob) -> dict[str, object]:
    return {
        "id": job.public_id,
        "job_type": job.job_type,
        "status": job.status,
        "progress": job.progress,
        "processed_count": job.processed_count,
        "failed_count": job.failed_count,
        "message": job.message,
        "result": job.result or {},
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _save_upload(
    upload: UploadFile,
    destination: Path,
    *,
    max_bytes: int | None = None,
) -> int:
    written = 0
    try:
        with destination.open("wb") as target:
            while chunk := upload.file.read(1024 * 1024):
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise ValueError(f"上传文件超过{max_bytes // 1024 // 1024}MB限制")
                target.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return written


def _gold_sample_dict(sample: GoldSample) -> dict[str, object]:
    reveal_annotations = sample.status in {"disputed", "agreed", "arbitrated"}
    appeal = sample.appeal
    predicted = appeal.annotation
    return {
        "id": sample.id,
        "status": sample.status,
        "appeal": {
            "id": appeal.id,
            "external_id": appeal.external_id,
            "quarter": appeal.quarter,
            "title": appeal.redacted_title,
            "content": appeal.redacted_content,
            "reply": appeal.redacted_reply or "",
            "predicted_l1": predicted.topic if predicted else "",
            "predicted_l2": predicted.subtopic if predicted else "",
        },
        "annotation_count": sum(
            item.role == "annotator" for item in sample.annotations
        ),
        "annotations": (
            [
                {
                    "annotator": item.annotator_key,
                    "role": item.role,
                    "l1": item.l1_label.name,
                    "l2": item.l2_label.name if item.l2_label else "",
                    "notes": item.notes,
                }
                for item in sample.annotations
            ]
            if reveal_annotations
            else []
        ),
        "final_l1": sample.final_l1_label.name if sample.final_l1_label else "",
        "final_l2": sample.final_l2_label.name if sample.final_l2_label else "",
        "finalized_by": sample.finalized_by,
    }


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, object]:
    return {
        "status": "ok",
        "version": app.version,
        "database": session.get_bind().dialect.name,
        "model_profile": active_profile().name,
        "taxonomy_status": settings.taxonomy_status,
    }


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(
        settings.base_dir / "app" / "static" / "favicon.svg",
        media_type="image/svg+xml",
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"app_name": settings.app_name, "error": "", "next": next},
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: Session = Depends(get_session),
):
    key = request.client.host if request.client else "unknown"
    now = monotonic()
    attempts = _login_attempts[key]
    while attempts and now - attempts[0] > 300:
        attempts.popleft()
    if len(attempts) >= 10:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "app_name": settings.app_name,
                "error": "登录尝试过多，请五分钟后重试。",
                "next": next,
            },
            status_code=429,
        )
    user = authenticate(session, username, password)
    if not user:
        attempts.append(now)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "app_name": settings.app_name,
                "error": "用户名或密码错误。",
                "next": next,
            },
            status_code=401,
        )
    attempts.clear()
    token = create_user_session(session, user)
    audit(session, user=user, action="login")
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(safe_next, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        max_age=12 * 3600,
    )
    return response


@app.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    user = getattr(request.state, "user", None)
    if user:
        delete_user_chat_sessions(session, user.id)
    revoke_token(session, request.cookies.get(COOKIE_NAME))
    audit(session, user=user, action="logout")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.post("/api/users")
def create_user(
    request: Request,
    payload: UserCreateRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    actor = _require_role(request, "admin")
    if session.scalar(select(User).where(User.username == payload.username.strip())):
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(
        username=payload.username.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    session.add(user)
    session.commit()
    audit(
        session,
        user=actor,
        action="create_user",
        resource_type="user",
        resource_id=str(user.id),
        metadata={"role": user.role},
    )
    return {"id": user.id, "username": user.username, "role": user.role}


@app.get("/", response_class=HTMLResponse)
@app.get("/map", response_class=HTMLResponse)
def map_page(
    request: Request,
    quarter: str | None = None,
    topic_l1: str | None = None,
    ctype: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    overview = map_overview(
        session,
        quarter=quarter,
        topic_l1=topic_l1,
        appeal_type=ctype,
    )
    context = _base_context(request, session)
    context.update(
        {
            "overview": overview,
            "selected_quarter": overview["quarter"] or "",
            "selected_topic": topic_l1 or "",
            "selected_ctype": ctype or "",
        }
    )
    return templates.TemplateResponse(request=request, name="map.html", context=context)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    city: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    quarter: str | None = Query(None),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    selected_city = city or settings.default_city
    stats = dashboard_stats(
        session,
        city=selected_city,
        start=start,
        end=end,
        quarter=quarter,
    )
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
            "selected_quarter": quarter or "",
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
    quarter: str | None = None,
    topic_l1: str | None = None,
    ctype: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return dashboard_stats(
        session,
        city=city,
        start=start,
        end=end,
        quarter=quarter,
        topic_l1=topic_l1,
        appeal_type=ctype,
    )


@app.get("/api/map")
@app.get("/api/map/overview")
def map_api(
    quarter: str | None = None,
    topic_l1: str | None = None,
    ctype: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return map_overview(
        session,
        quarter=quarter,
        topic_l1=topic_l1,
        appeal_type=ctype,
    )


@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    context = _base_context(request, session)
    context.update(
        {
            "selected_city": "",
            "selected_quarter": context["quarters"][0] if context["quarters"] else "",
        }
    )
    return templates.TemplateResponse(request=request, name="ask.html", context=context)


@app.post("/api/chat/sessions")
def chat_session_create(
    request: Request,
    payload: ChatSessionCreateRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = getattr(request.state, "user", None)
    chat = create_chat_session(
        session,
        context={
            "city": payload.city.strip(),
            "quarter": payload.quarter.strip(),
            "topic_l1": payload.topic_l1.strip(),
            "appeal_type": payload.appeal_type.strip(),
        },
        user_id=user.id if user else None,
    )
    return {
        "id": chat.public_id,
        "expires_at": chat.expires_at.isoformat() if chat.expires_at else None,
        "context": chat.context,
    }


@app.post("/api/chat/sessions/{chat_id}/messages/stream")
def chat_message_stream(
    request: Request,
    chat_id: str,
    payload: ChatMessageRequest,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    user = getattr(request.state, "user", None)
    chat = get_active_chat_session(session, chat_id, user_id=user.id if user else None)
    if not chat:
        raise HTTPException(status_code=404, detail="会话不存在或已超时")

    def stream():
        answer = ""
        source = "deterministic-tools"
        with SessionLocal() as stream_session:
            active_chat = get_active_chat_session(
                stream_session,
                chat_id,
                user_id=user.id if user else None,
            )
            if not active_chat:
                yield _sse("error", {"detail": "会话不存在或已超时"})
                return
            yield _sse("status", {"title": "解析问题", "detail": "正在确定统计范围和指标"})
            try:
                turn = prepare_chat_turn(stream_session, active_chat, payload)
            except Exception as exc:
                yield _sse("error", {"detail": str(exc)})
                return
            yield _sse(
                "plan",
                {
                    "query_plan": turn.plan.model_dump(),
                    "evidence_count": len(turn.evidence),
                },
            )
            if turn.provider.enabled and turn.use_provider and turn.plan.intent != "unsupported":
                source = f"{turn.provider.model_name} + verified-tools"
                yield _sse("status", {"title": "组织回答", "detail": "正在根据事实和证据生成"})
                try:
                    pieces: list[str] = []
                    for piece in turn.provider.stream_complete(
                        turn.system_prompt,
                        turn.user_prompt,
                        purpose="chat",
                        prompt_version="chat-query-plan-v1",
                    ):
                        pieces.append(piece)
                        yield _sse("delta", {"text": piece})
                    answer = "".join(pieces)
                except Exception:
                    answer = turn.local_answer
                    source = "deterministic-tools (model fallback)"
                    yield _sse("reset", {"text": answer})
            else:
                answer = turn.local_answer
                yield _sse("delta", {"text": answer})
            save_chat_turn(
                stream_session,
                active_chat,
                request=payload,
                turn=turn,
                answer=answer,
            )
            yield _sse(
                "done",
                {
                    "answer_source": source,
                    "answer_html": render_markdown(answer),
                    "query_plan": turn.plan.model_dump(),
                    "evidence": [item.model_dump() for item in turn.evidence],
                    "expires_at": active_chat.expires_at.isoformat()
                    if active_chat.expires_at
                    else None,
                },
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete("/api/chat/sessions/{chat_id}", status_code=204)
def chat_session_delete(
    request: Request,
    chat_id: str,
    session: Session = Depends(get_session),
) -> Response:
    user = getattr(request.state, "user", None)
    chat = get_active_chat_session(session, chat_id, user_id=user.id if user else None)
    if not chat:
        raise HTTPException(status_code=404, detail="会话不存在")
    delete_chat_session(session, chat)
    return Response(status_code=204)


@app.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    selected: str | None = None,
    city_code: str = "",
    city: str = "",
    quarter: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    statement = select(ReportDocument).order_by(ReportDocument.updated_at.desc()).limit(50)
    if city_code:
        statement = statement.where(ReportDocument.city_code == city_code)
    if quarter:
        statement = statement.where(ReportDocument.quarter == quarter)
    reports = list(session.scalars(statement).all())
    report = (
        session.scalar(select(ReportDocument).where(ReportDocument.public_id == selected))
        if selected
        else (reports[0] if reports else None)
    )
    context = _base_context(request, session)
    context.update(
        {
            "reports": reports,
            "report": report,
            "report_html": render_markdown(report.current_content) if report else "",
            "selected_city_code": city_code,
            "selected_city": city,
            "selected_quarter": quarter or (context["quarters"][0] if context["quarters"] else ""),
            "policies": list(
                session.scalars(
                    select(PolicyDocument)
                    .where(PolicyDocument.status == "active")
                    .order_by(PolicyDocument.published_at.desc())
                    .limit(50)
                ).all()
            ),
        }
    )
    return templates.TemplateResponse(request=request, name="reports.html", context=context)


@app.post("/api/reports", status_code=202)
def report_create(
    request: Request,
    payload: ReportCreateRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "researcher")
    job = create_job(session, "report", payload.model_dump())
    audit(
        session,
        user=user,
        action="create_report_job",
        resource_type="analysis_job",
        resource_id=job.public_id,
        metadata={"report_type": payload.report_type, "quarter": payload.quarter},
    )
    return _job_dict(job)


@app.get("/api/reports/{report_id}")
def report_get(report_id: str, session: Session = Depends(get_session)) -> dict[str, object]:
    report = session.scalar(
        select(ReportDocument).where(ReportDocument.public_id == report_id)
    )
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {
        "id": report.public_id,
        "report_type": report.report_type,
        "mode": report.mode,
        "title": report.title,
        "quarter": report.quarter,
        "city_code": report.city_code,
        "city": report.city,
        "status": report.status,
        "content": report.current_content,
        "content_html": render_markdown(report.current_content),
        "fact_pack": report.fact_pack,
        "generated_by": report.generated_by,
        "updated_at": report.updated_at.isoformat(),
    }


@app.put("/api/reports/{report_id}")
def report_update(
    request: Request,
    report_id: str,
    payload: ReportUpdateRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "researcher")
    report = session.scalar(
        select(ReportDocument).where(ReportDocument.public_id == report_id)
    )
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    try:
        update_report(
            session,
            report,
            payload.content,
            change_note=payload.change_note,
            editor_id=user.id if user else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(
        session,
        user=user,
        action="edit_report",
        resource_type="report",
        resource_id=report.public_id,
    )
    return {"id": report.public_id, "status": report.status, "updated_at": report.updated_at}


@app.post("/api/reports/{report_id}/publish")
def report_publish(
    request: Request,
    report_id: str,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "reviewer")
    report = session.scalar(
        select(ReportDocument).where(ReportDocument.public_id == report_id)
    )
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    try:
        publish_report(session, report, publisher_id=user.id if user else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        session,
        user=user,
        action="publish_report",
        resource_type="report",
        resource_id=report.public_id,
    )
    return {"id": report.public_id, "status": report.status, "published_at": report.published_at}


@app.get("/api/reports/{report_id}/export")
def report_export(
    report_id: str,
    format: str = Query("docx", pattern="^(docx|pdf)$"),
    session: Session = Depends(get_session),
) -> FileResponse:
    report = session.scalar(
        select(ReportDocument).where(ReportDocument.public_id == report_id)
    )
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    if report.status != "published":
        raise HTTPException(status_code=409, detail="只有已发布版本可以导出")
    path = export_report_docx(report) if format == "docx" else export_report_pdf(report)
    filename = quote(f"{report.title}.{format}")
    media_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if format == "docx"
        else "application/pdf"
    )
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@app.post("/api/policies")
def policy_create(
    request: Request,
    payload: PolicyCreateRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "researcher")
    content = payload.content
    archived_path = ""
    if payload.source_url and not content:
        if not settings.external_search_enabled:
            raise HTTPException(
                status_code=409,
                detail="链接抓取未启用；请上传政策文件或设置 EXTERNAL_SEARCH_ENABLED=true",
            )
        try:
            content, raw, suffix = fetch_policy_url(payload.source_url)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        digest = hashlib.sha256(raw).hexdigest()
        target = settings.archive_dir / "policies" / f"{digest}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(raw)
        archived_path = str(target)
    policy = create_policy(
        session,
        **payload.model_dump(exclude={"content"}),
        content=content,
        archived_path=archived_path,
    )
    audit(
        session,
        user=user,
        action="create_policy",
        resource_type="policy",
        resource_id=str(policy.id),
    )
    return {"id": policy.id, "title": policy.title, "version": policy.version}


@app.post("/api/policies/upload")
def policy_upload(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    issuing_authority: str = Form(""),
    applicable_region: str = Form("全国"),
    source_url: str = Form(""),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "researcher")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt", ".md"}:
        raise HTTPException(status_code=400, detail="政策材料仅支持 PDF、DOCX、TXT 和 Markdown")
    temporary = settings.uploads_dir / f"{uuid.uuid4().hex}{suffix}"
    try:
        try:
            _save_upload(file, temporary, max_bytes=MAX_POLICY_BYTES)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        content = extract_policy_file(temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        archived = settings.archive_dir / "policies" / f"{digest}{suffix}"
        archived.parent.mkdir(parents=True, exist_ok=True)
        if not archived.exists():
            shutil.copy2(temporary, archived)
        policy = create_policy(
            session,
            title=title,
            issuing_authority=issuing_authority,
            applicable_region=applicable_region,
            source_url=source_url,
            content=content,
            archived_path=str(archived),
        )
    finally:
        temporary.unlink(missing_ok=True)
    audit(
        session,
        user=user,
        action="upload_policy",
        resource_type="policy",
        resource_id=str(policy.id),
    )
    return {"id": policy.id, "title": policy.title, "version": policy.version}


def _allowed_import_path(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    allowed = (settings.data_dir.resolve(), settings.uploads_dir.resolve())
    if not any(path == root or root in path.parents for root in allowed):
        raise HTTPException(status_code=400, detail="导入文件必须位于 data 或 uploads 目录")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="导入文件不存在")
    return path


@app.post("/api/import-batches", status_code=202)
def import_batch_create(
    request: Request,
    payload: ImportBatchRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "admin")
    path = _allowed_import_path(payload.path)
    job_payload = payload.model_dump()
    job_payload["path"] = str(path)
    job = create_job(session, "import", job_payload)
    audit(
        session,
        user=user,
        action="create_import_job",
        resource_type="analysis_job",
        resource_id=job.public_id,
        metadata={"filename": path.name},
    )
    return _job_dict(job)


@app.post("/api/snapshots", status_code=202)
def snapshot_create(
    request: Request,
    payload: SnapshotRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "admin")
    job = create_job(session, "snapshot", payload.model_dump())
    audit(
        session,
        user=user,
        action="create_snapshot_job",
        resource_type="analysis_job",
        resource_id=job.public_id,
        metadata={"quarter": payload.quarter},
    )
    return _job_dict(job)


@app.get("/api/jobs/{job_id}")
def job_get(job_id: str, session: Session = Depends(get_session)) -> dict[str, object]:
    job = session.scalar(select(AnalysisJob).where(AnalysisJob.public_id == job_id))
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _job_dict(job)


@app.patch("/api/regions/{region_id}")
def region_update(
    request: Request,
    region_id: int,
    payload: RegionMetadataUpdate,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "admin")
    region = session.get(Region, region_id)
    if not region:
        raise HTTPException(status_code=404, detail="地区不存在")
    for key, value in payload.model_dump().items():
        setattr(region, key, value)
    if payload.prefecture_city:
        region.prefecture_city = payload.prefecture_city
    session.commit()
    clear_dashboard_cache()
    audit(
        session,
        user=user,
        action="update_region_mapping",
        resource_type="region",
        resource_id=str(region.id),
    )
    return {"id": region.id, "city": region.city, **payload.model_dump()}


@app.get("/api/taxonomy")
def taxonomy_get(session: Session = Depends(get_session)) -> dict[str, object]:
    version = session.scalar(
        select(TaxonomyVersion)
        .where(TaxonomyVersion.status.in_(["published", "trial"]))
        .order_by(TaxonomyVersion.id.desc())
    ) or ensure_draft_taxonomy(session)
    labels = session.scalars(
        select(TaxonomyLabel)
        .where(TaxonomyLabel.taxonomy_version_id == version.id)
        .order_by(TaxonomyLabel.level, TaxonomyLabel.sort_order)
    ).all()
    allowed, failures = can_publish(version)
    return {
        "id": version.id,
        "version": version.version,
        "status": version.status,
        "gold_sample_size": version.gold_sample_size,
        "l1_macro_f1": version.l1_macro_f1,
        "l2_macro_f1": version.l2_macro_f1,
        "publishable": allowed,
        "publish_failures": failures,
        "labels": [
            {
                "id": label.id,
                "parent_id": label.parent_id,
                "level": label.level,
                "name": label.name,
                "definition": label.definition,
                "status": label.status,
            }
            for label in labels
        ],
    }


@app.put("/api/taxonomy/labels/{label_id}")
def taxonomy_label_update(
    request: Request,
    label_id: int,
    payload: TaxonomyLabelUpdate,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "admin")
    label = session.get(TaxonomyLabel, label_id)
    if not label:
        raise HTTPException(status_code=404, detail="标签不存在")
    version = session.get(TaxonomyVersion, label.taxonomy_version_id)
    if not version or version.status == "published":
        raise HTTPException(status_code=409, detail="已发布标签版本不可直接修改")
    label.name = payload.name.strip()
    label.definition = payload.definition.strip()
    label.status = payload.status
    if payload.include_examples is not None:
        label.include_examples = [
            item.strip() for item in payload.include_examples if item.strip()
        ]
    if payload.exclude_examples is not None:
        label.exclude_examples = [
            item.strip() for item in payload.exclude_examples if item.strip()
        ]
    session.commit()
    audit(
        session,
        user=user,
        action="review_taxonomy_label",
        resource_type="taxonomy_label",
        resource_id=str(label.id),
        metadata={"status": label.status},
    )
    return {
        "id": label.id,
        "name": label.name,
        "definition": label.definition,
        "status": label.status,
    }


@app.post("/api/taxonomy/{version_id}/gold-samples", status_code=201)
def taxonomy_gold_samples_create(
    request: Request,
    version_id: int,
    payload: GoldSampleCreateRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "admin")
    version = session.get(TaxonomyVersion, version_id)
    if not version:
        raise HTTPException(status_code=404, detail="标签版本不存在")
    try:
        created = create_gold_samples(session, version, payload.appeal_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        session,
        user=user,
        action="create_gold_samples",
        resource_type="taxonomy",
        resource_id=str(version.id),
        metadata={"created_count": len(created)},
    )
    return {"created_count": len(created), "ids": [item.id for item in created]}


@app.get("/api/taxonomy/{version_id}/gold-samples")
def taxonomy_gold_samples_list(
    request: Request,
    version_id: int,
    status: str = "",
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    _require_role(request, "researcher")
    version = session.get(TaxonomyVersion, version_id)
    if not version:
        raise HTTPException(status_code=404, detail="标签版本不存在")
    samples = list_gold_samples(session, version_id, status=status, limit=limit)
    return {
        "taxonomy_version": version.version,
        "items": [_gold_sample_dict(item) for item in samples],
    }


@app.post("/api/taxonomy/{version_id}/gold-samples/{sample_id}/annotations")
def taxonomy_gold_annotation_create(
    request: Request,
    version_id: int,
    sample_id: int,
    payload: GoldAnnotationRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "researcher")
    sample = get_gold_sample(session, version_id, sample_id)
    if not sample:
        raise HTTPException(status_code=404, detail="黄金样本不存在")
    annotator_key = user.username if user else payload.annotator_name
    try:
        submit_gold_annotation(
            session,
            sample,
            annotator_key=annotator_key,
            user=user,
            l1_label_id=payload.l1_label_id,
            l2_label_id=payload.l2_label_id,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        session,
        user=user,
        action="annotate_gold_sample",
        resource_type="gold_sample",
        resource_id=str(sample.id),
    )
    refreshed = get_gold_sample(session, version_id, sample_id)
    assert refreshed is not None
    return _gold_sample_dict(refreshed)


@app.post("/api/taxonomy/{version_id}/gold-samples/{sample_id}/arbitrate")
def taxonomy_gold_sample_arbitrate(
    request: Request,
    version_id: int,
    sample_id: int,
    payload: GoldAnnotationRequest,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "reviewer")
    sample = get_gold_sample(session, version_id, sample_id)
    if not sample:
        raise HTTPException(status_code=404, detail="黄金样本不存在")
    arbitrator_key = user.username if user else payload.annotator_name
    try:
        arbitrate_gold_sample(
            session,
            sample,
            arbitrator_key=arbitrator_key,
            user=user,
            l1_label_id=payload.l1_label_id,
            l2_label_id=payload.l2_label_id,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        session,
        user=user,
        action="arbitrate_gold_sample",
        resource_type="gold_sample",
        resource_id=str(sample.id),
    )
    refreshed = get_gold_sample(session, version_id, sample_id)
    assert refreshed is not None
    return _gold_sample_dict(refreshed)


@app.put("/api/taxonomy/{version_id}/metrics")
def taxonomy_metrics(
    request: Request,
    version_id: int,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "admin")
    version = session.get(TaxonomyVersion, version_id)
    if not version:
        raise HTTPException(status_code=404, detail="标签版本不存在")
    metrics = recompute_taxonomy_metrics(session, version)
    audit(
        session,
        user=user,
        action="recompute_taxonomy_metrics",
        resource_type="taxonomy",
        resource_id=str(version.id),
        metadata=metrics,
    )
    allowed, failures = can_publish(version)
    return {"publishable": allowed, "failures": failures, **metrics}


@app.post("/api/taxonomy/{version_id}/publish")
def taxonomy_publish(
    request: Request,
    version_id: int,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    user = _require_role(request, "admin")
    version = session.get(TaxonomyVersion, version_id)
    if not version:
        raise HTTPException(status_code=404, detail="标签版本不存在")
    try:
        publish_taxonomy(session, version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(
        session,
        user=user,
        action="publish_taxonomy",
        resource_type="taxonomy",
        resource_id=str(version.id),
    )
    return {"id": version.id, "version": version.version, "status": version.status}


@app.get("/data", response_class=HTMLResponse)
def data_page(
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    batches = list(
        session.scalars(
            select(ImportBatch).order_by(ImportBatch.imported_at.desc()).limit(50)
        ).all()
    )
    jobs = list(
        session.scalars(
            select(AnalysisJob).order_by(AnalysisJob.created_at.desc()).limit(30)
        ).all()
    )
    snapshots = list(
        session.scalars(
            select(QuarterSnapshot)
            .order_by(QuarterSnapshot.quarter.desc(), QuarterSnapshot.version.desc())
            .limit(30)
        ).all()
    )
    context = _base_context(request, session)
    taxonomy = context["taxonomy"] or ensure_draft_taxonomy(session)
    taxonomy_labels = list(
        session.scalars(
            select(TaxonomyLabel)
            .where(TaxonomyLabel.taxonomy_version_id == taxonomy.id)
            .order_by(TaxonomyLabel.level, TaxonomyLabel.sort_order, TaxonomyLabel.id)
        ).all()
    )
    gold_status_counts = dict(
        session.execute(
            select(GoldSample.status, func.count(GoldSample.id))
            .where(GoldSample.taxonomy_version_id == taxonomy.id)
            .group_by(GoldSample.status)
        ).all()
    )
    gold_queue = [
        item
        for item in list_gold_samples(session, taxonomy.id, limit=100)
        if item.status not in {"agreed", "arbitrated"}
    ][:50]
    context.update(
        {
            "batches": batches,
            "jobs": jobs,
            "snapshots": snapshots,
            "taxonomy": taxonomy,
            "taxonomy_labels": taxonomy_labels,
            "taxonomy_l1_labels": [item for item in taxonomy_labels if item.level == 1],
            "taxonomy_l2_labels": [item for item in taxonomy_labels if item.level == 2],
            "gold_status_counts": gold_status_counts,
            "gold_queue": gold_queue,
            "message": message,
        }
    )
    return templates.TemplateResponse(request=request, name="data.html", context=context)


@app.post("/data/import")
def upload_data(
    request: Request,
    file: UploadFile = File(...),
    province: str = Form(...),
    city: str = Form(...),
    district: str = Form(""),
    source_platform_code: str = Form(""),
    source_platform_name: str = Form(""),
    city_code: str = Form(""),
    district_code: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    user = _require_role(request, "admin")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".xlsx", ".xls", ".csv", ".parquet"}:
        raise HTTPException(status_code=400, detail="仅支持 Excel、CSV 和 Parquet")
    destination = settings.uploads_dir / f"{uuid.uuid4().hex}{suffix}"
    _save_upload(file, destination)
    job = create_job(
        session,
        "import",
        {
            "path": str(destination),
            "province": province,
            "city": city,
            "district": district,
            "source_platform_code": source_platform_code,
            "source_platform_name": source_platform_name or (file.filename or ""),
            "city_code": city_code,
            "district_code": district_code,
        },
    )
    audit(
        session,
        user=user,
        action="upload_import_file",
        resource_type="analysis_job",
        resource_id=job.public_id,
        metadata={"filename": file.filename or ""},
    )
    return RedirectResponse(
        url=f"/data?message={quote('已创建导入任务：' + job.public_id)}",
        status_code=303,
    )
