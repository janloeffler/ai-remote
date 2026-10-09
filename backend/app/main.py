import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from jinja2 import pass_context
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import ai_tools, allowlist, db, i18n, rate_limit, settings
from .auth import require_api_key, require_session, secrets_match, session_binding
from .datetime_filter import format_datetime
from .markdown_filter import render_markdown
from .models import CommandRequest, JobCompleteRequest, NewSessionCommandRequest, SyncIndexRequest

APP_DIR = Path(__file__).parent
TOOL_LABELS = {ai_tools.CLAUDE_CODE: "Claude Code", ai_tools.CURSOR: "Cursor"}

# No interactive docs: they were served unauthenticated (SEC-011), handing any visitor
# the full route map — including the agent-only endpoints and their auth scheme — for a
# service whose payoff is command execution on the owner's machine. Set
# ENABLE_API_DOCS=true locally if you want /docs back while developing.
_docs_enabled = os.environ.get("ENABLE_API_DOCS", "false").lower() == "true"
app = FastAPI(
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
_cookie_https_only = os.environ.get("SESSION_COOKIE_HTTPS_ONLY", "true").lower() == "true"
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, https_only=_cookie_https_only)


@app.middleware("http")
async def _resolve_language(request: Request, call_next):
    # Explicit choice (cookie) > browser Accept-Language > English. Not secret, so it is
    # resolved for every request, including the login page.
    request.state.lang = i18n.resolve_language(
        request.cookies.get(i18n.LANGUAGE_COOKIE), request.headers.get("accept-language")
    )
    return await call_next(request)


templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["markdown"] = render_markdown


@pass_context
def _t(context, key: str, **values) -> str:
    return i18n.translate(context["request"].state.lang, key, **values)


@pass_context
def _localdt(context, iso_timestamp: str | None) -> str:
    return format_datetime(iso_timestamp, context["request"].state.lang)


@pass_context
def _js_strings(context) -> dict:
    return i18n.js_catalog(context["request"].state.lang)


@pass_context
def _ai_tools_error(context) -> str | None:
    if settings.ENABLED_TOOLS:
        return None
    return i18n.translate(context["request"].state.lang, "tools.none_enabled")


templates.env.globals["t"] = _t
templates.env.globals["ai_tools_error"] = _ai_tools_error
templates.env.globals["tool_label"] = lambda tool: TOOL_LABELS.get(tool, tool)
templates.env.globals["js_strings"] = _js_strings
templates.env.filters["localdt"] = _localdt
templates.env.globals["build_timestamp"] = os.environ.get("BUILD_TIMESTAMP", "dev")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup() -> None:
    conn = db.get_connection(os.environ.get("DATABASE_PATH", "app.db"))
    db.init_db(conn)
    conn.close()


@app.get("/jobs/pending", dependencies=[Depends(require_api_key)])
def jobs_pending(conn=Depends(db.get_db_dependency)):
    db.fail_stale_jobs(conn)
    jobs = db.claim_pending_jobs(conn)
    db.record_agent_contact(conn)
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    return {"jobs": jobs, "poll_interval_seconds": interval}


@app.post("/jobs/{job_id}/complete", dependencies=[Depends(require_api_key)])
def jobs_complete(job_id: str, body: JobCompleteRequest, conn=Depends(db.get_db_dependency)):
    messages = [m.model_dump() for m in body.messages]
    db.complete_job(conn, job_id, body.status, body.result_text, messages, body.is_complete)
    return {"ok": True}


def _compute_eta_seconds(last_contact: str | None, interval_seconds: int) -> int:
    """Rough ETA until the agent's next check-in, clamped to [0, interval_seconds].

    Computed entirely from this process's own clock — `last_contact` is a
    timestamp this same backend wrote in `db.record_agent_contact`, so there's
    no client-clock skew to worry about.
    """
    if last_contact is None:
        return interval_seconds
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_contact)).total_seconds()
    return max(0, min(interval_seconds, round(interval_seconds - elapsed)))


def _poll_mode(conn) -> tuple[str, int]:
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    mode = "active" if db.active_window_open(conn) else "default"
    return mode, interval


@app.post("/chats/{session_id}/fetch-full", dependencies=[Depends(require_session)])
def fetch_full(session_id: str, full: bool = False, conn=Depends(db.get_db_dependency)):
    # Only ever queue a job for a session we actually know about: the target string is
    # handed to the agent, which turns it into a filesystem lookup, so this route must
    # not be a way to inject arbitrary target strings into that path (SEC-005).
    session = _get_visible_session(conn, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if full:
        job_id = db.create_job(conn, "fetch_full", session_id)
    else:
        current = session["loaded_message_count"]
        next_count = current + settings.CHAT_HISTORY_PAGE_SIZE
        job_id = db.create_job(conn, "fetch_full", session_id, payload=json.dumps({"count": next_count}))
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), interval)
    # No explicit bump here: require_session already extends the active window for
    # every authenticated request (see auth.py), this route included.
    return {"job_id": job_id, "eta_seconds": eta_seconds}


@app.get("/chats/{session_id}/status", dependencies=[Depends(require_session)])
def job_status(session_id: str, job_id: str, conn=Depends(db.get_db_dependency)):
    job = db.get_job(conn, job_id)
    return {"status": job["status"] if job else "unknown"}


@app.post("/sync/index", dependencies=[Depends(require_api_key)])
def sync_index(body: SyncIndexRequest, conn=Depends(db.get_db_dependency)):
    received = 0
    for session in body.sessions:
        if session.tool not in settings.ENABLED_TOOLS:
            continue
        received += 1
        db.upsert_session(conn, session.model_dump(exclude={"recent_messages"}))
        db.apply_recent_messages(conn, session.id, [m.model_dump() for m in session.recent_messages])
    db.record_agent_contact(conn)
    return {"received": received}


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, api_key: str = Form(...)):
    retry_after = rate_limit.seconds_until_unlocked(request)
    if retry_after is not None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": i18n.translate(request.state.lang, "login.throttled", n=int(retry_after) + 1)},
            status_code=429,
        )
    if secrets_match(api_key, settings.API_KEY):
        rate_limit.record_success(request)
        request.session["authenticated"] = True
        request.session["binding"] = session_binding()
        return RedirectResponse("/", status_code=303)
    rate_limit.record_failure(request)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": i18n.translate(request.state.lang, "login.invalid")},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    # POST only: SameSite=Lax cookies are not sent on cross-site POSTs, so a third-party
    # page can't log the owner out with a link or image.
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _expand_home_dir(value: str) -> str:
    if value == "~":
        return settings.LOCAL_HOME_DIR
    if value.startswith("~/"):
        return settings.LOCAL_HOME_DIR + "/" + value[2:]
    return value


def _get_visible_session(conn, session_id: str) -> dict | None:
    """A session of a disabled tool is treated as nonexistent everywhere it could surface."""
    session = db.get_session(conn, session_id)
    if session is not None and session["tool"] not in settings.ENABLED_TOOLS:
        return None
    return session


@app.get("/", dependencies=[Depends(require_session)])
def list_chats(
    request: Request,
    tool: str | None = None,
    project: str | None = None,
    group: str | None = None,
    q: str | None = None,
    sort: str = "date_desc",
    limit: int = 100,
    conn=Depends(db.get_db_dependency),
):
    expanded_project = _expand_home_dir(project) if project else project
    sessions = db.get_sessions(
        conn, tool=tool, tools=settings.ENABLED_TOOLS, project=expanded_project, date_group=group, q=q, limit=limit, sort=sort
    )
    poll_mode, poll_interval_seconds = _poll_mode(conn)
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "sessions": sessions,
            "tool": tool,
            "project": project,
            "group": group,
            "q": q,
            "sort": sort,
            "last_agent_contact": db.get_last_agent_contact(conn),
            "project_paths": db.get_distinct_project_paths(conn, settings.ENABLED_TOOLS),
            "enabled_tools": settings.ENABLED_TOOLS,
            "local_home_dir": settings.LOCAL_HOME_DIR,
            "remote_commands_paused": db.get_remote_commands_paused(conn),
            "poll_mode": poll_mode,
            "poll_interval_seconds": poll_interval_seconds,
        },
    )


@app.post("/settings/pause-remote-commands", dependencies=[Depends(require_session)])
def toggle_pause_remote_commands(paused: bool = Form(...), conn=Depends(db.get_db_dependency)):
    db.set_remote_commands_paused(conn, paused)
    return RedirectResponse("/", status_code=303)


def _settings_context(conn, **extra) -> dict:
    stored_default, stored_active = db.get_poll_interval_overrides(conn)
    poll_mode, poll_interval_seconds = _poll_mode(conn)
    return {
        "interval_default": "" if stored_default is None else stored_default,
        "interval_active": "" if stored_active is None else stored_active,
        "env_interval_default": settings.AI_REMOTE_INTERVAL_SECONDS,
        "env_interval_active": settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS,
        "active_minutes": settings.ACTIVE_INTERVAL_DURATION_MIN,
        "standard_range": settings.STANDARD_INTERVAL_RANGE,
        "active_range": settings.ACTIVE_INTERVAL_RANGE,
        "remote_commands_paused": db.get_remote_commands_paused(conn),
        "poll_mode": poll_mode,
        "poll_interval_seconds": poll_interval_seconds,
        "errors": [],
        "saved": False,
        **extra,
    }


@app.get("/settings", dependencies=[Depends(require_session)])
def settings_page(request: Request, saved: bool = False, conn=Depends(db.get_db_dependency)):
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            conn,
            saved=saved,
            language=request.cookies.get(i18n.LANGUAGE_COOKIE)
            if request.cookies.get(i18n.LANGUAGE_COOKIE) in i18n.SUPPORTED
            else "auto",
        ),
    )


def _parse_interval(raw: str, label: str, bounds: tuple[int, int], lang: str, errors: list[str]) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    low, high = bounds
    if not raw.isascii() or not raw.isdigit() or not low <= int(raw) <= high:
        errors.append(i18n.translate(lang, "settings.err.range", label=label, lo=low, hi=high))
        return None
    return int(raw)


@app.post("/settings", dependencies=[Depends(require_session)])
def settings_save(
    request: Request,
    language: str = Form("auto"),
    interval_default: str = Form(""),
    interval_active: str = Form(""),
    conn=Depends(db.get_db_dependency),
):
    lang = request.state.lang
    errors: list[str] = []
    if language != "auto" and language not in i18n.SUPPORTED:
        errors.append(i18n.translate(lang, "settings.err.language"))
    standard = _parse_interval(
        interval_default, i18n.translate(lang, "settings.interval_default"),
        settings.STANDARD_INTERVAL_RANGE, lang, errors,
    )
    active = _parse_interval(
        interval_active, i18n.translate(lang, "settings.interval_active"),
        settings.ACTIVE_INTERVAL_RANGE, lang, errors,
    )
    if not errors:
        effective_standard = standard if standard is not None else settings.AI_REMOTE_INTERVAL_SECONDS
        effective_active = active if active is not None else settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
        if effective_active > effective_standard:
            errors.append(i18n.translate(lang, "settings.err.order"))
    if errors:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                conn,
                errors=errors,
                language=language if language in i18n.SUPPORTED else "auto",
                interval_default=interval_default,
                interval_active=interval_active,
            ),
            status_code=422,
        )
    db.set_poll_interval_overrides(conn, standard, active)
    response = RedirectResponse("/settings?saved=1", status_code=303)
    if language == "auto":
        response.delete_cookie(i18n.LANGUAGE_COOKIE)
    else:
        response.set_cookie(
            i18n.LANGUAGE_COOKIE, language, max_age=365 * 24 * 3600, samesite="lax",
            httponly=True, secure=_cookie_https_only,
        )
    return response


@app.get("/chats/{session_id}", dependencies=[Depends(require_session)])
def chat_detail(request: Request, session_id: str, conn=Depends(db.get_db_dependency)):
    session = _get_visible_session(conn, session_id)
    poll_mode, poll_interval_seconds = _poll_mode(conn)
    if session is None:
        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "session": None,
                "messages": [],
                "remote_commands_paused": db.get_remote_commands_paused(conn),
                "poll_mode": poll_mode,
                "poll_interval_seconds": poll_interval_seconds,
            },
            status_code=404,
        )
    messages = db.get_messages(conn, session_id) if session["loaded_message_count"] > 0 else []
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "session": session,
            "messages": messages,
            "command_allowed": allowlist.is_allowed(session["project_path"]),
            "remote_commands_paused": db.get_remote_commands_paused(conn),
            "poll_mode": poll_mode,
            "poll_interval_seconds": poll_interval_seconds,
        },
    )


@app.post("/chats/{session_id}/command", dependencies=[Depends(require_session)])
def send_command(session_id: str, body: CommandRequest, conn=Depends(db.get_db_dependency)):
    session = _get_visible_session(conn, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if db.get_remote_commands_paused(conn):
        raise HTTPException(status_code=409, detail="remote commands are paused")
    if not allowlist.is_allowed(session["project_path"]):
        raise HTTPException(status_code=403, detail="project path is not allow-listed")
    job_id = db.create_job(
        conn, "resume_message", session_id, payload=json.dumps({"prompt": body.prompt})
    )
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), interval)
    # No explicit bump here: require_session already extends the active window for
    # every authenticated request (see auth.py), this route included.
    return {"job_id": job_id, "eta_seconds": eta_seconds}


@app.get("/projects/new", dependencies=[Depends(require_session)])
def new_session_form(request: Request, conn=Depends(db.get_db_dependency)):
    poll_mode, poll_interval_seconds = _poll_mode(conn)
    return templates.TemplateResponse(
        request,
        "new_session.html",
        {
            "allowed_projects": settings.ALLOWED_PROJECTS,
            "enabled_tools": settings.ENABLED_TOOLS,
            "default_tool": settings.DEFAULT_TOOL,
            "remote_commands_paused": db.get_remote_commands_paused(conn),
            "poll_mode": poll_mode,
            "poll_interval_seconds": poll_interval_seconds,
        },
    )


@app.post("/projects/command", dependencies=[Depends(require_session)])
def send_new_session_command(body: NewSessionCommandRequest, conn=Depends(db.get_db_dependency)):
    if db.get_remote_commands_paused(conn):
        raise HTTPException(status_code=409, detail="remote commands are paused")
    if body.tool not in settings.ENABLED_TOOLS:
        raise HTTPException(status_code=403, detail="tool is disabled")
    if not allowlist.is_allowed(body.project_path):
        raise HTTPException(status_code=403, detail="project path is not allow-listed")
    job_id = db.create_job(
        conn,
        "new_session",
        body.project_path,
        payload=json.dumps({"prompt": body.prompt, "tool": body.tool}),
    )
    interval = db.current_poll_interval_seconds(
        conn, settings.AI_REMOTE_INTERVAL_SECONDS, settings.AI_REMOTE_ACTIVE_INTERVAL_SECONDS
    )
    eta_seconds = _compute_eta_seconds(db.get_last_agent_contact(conn), interval)
    # No explicit bump here: require_session already extends the active window for
    # every authenticated request (see auth.py), this route included.
    return {"job_id": job_id, "eta_seconds": eta_seconds}


@app.get("/jobs/{job_id}/status", dependencies=[Depends(require_session)])
def job_status_generic(job_id: str, conn=Depends(db.get_db_dependency)):
    job = db.get_job(conn, job_id)
    return {"status": job["status"] if job else "unknown", "result_text": job["result_text"] if job else None}


@app.get("/jobs", dependencies=[Depends(require_session)])
def jobs_audit_log(request: Request, conn=Depends(db.get_db_dependency)):
    poll_mode, poll_interval_seconds = _poll_mode(conn)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": db.get_all_jobs(conn),
            "remote_commands_paused": db.get_remote_commands_paused(conn),
            "poll_mode": poll_mode,
            "poll_interval_seconds": poll_interval_seconds,
        },
    )
