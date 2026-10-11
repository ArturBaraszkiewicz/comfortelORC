from __future__ import annotations

import hmac
import os
import secrets
import shutil
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .processes import InstanceLock
from .auth import Auth, COOKIE, SESSION_SECONDS
from .providers import availability, executable
from .runner import Runner, git, validate_project
from .store import Store, TERMINAL
from .integrations import clone_remote, project_commit, integration_status, jira_issue

ROOT = Path(__file__).resolve().parent.parent


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    path: str = Field(default="", max_length=1000)
    remote_url: str = Field(default="", max_length=1000)
    branch: str = Field(default="main", max_length=200)
    instructions: str = Field(default="", max_length=12000)


class JobInput(BaseModel):
    project_id: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=24000)
    provider: Literal["claude", "codex"] = "claude"
    model: str = Field(default="", max_length=120, pattern=r"^[a-zA-Z0-9._:/-]*$")
    kind: Literal["analysis", "code"] = "analysis"
    timeout_minutes: int = Field(default=20, ge=1, le=120)


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1000)


def create_app(data_dir: Path | None = None, *, start_worker=True, command_factory=None, public_url=None):
    data_dir = (data_dir or Path(os.environ.get("LOCAL_ORCHESTRATOR_DATA", str(ROOT / "data")))).resolve()
    store = Store(data_dir / "orchestrator.db")
    auth = Auth(store)
    public_url = (public_url or os.environ.get("LOCAL_ORCHESTRATOR_PUBLIC_URL", "")).rstrip("/")
    public = urlparse(public_url)
    if public_url and (public.scheme not in {"http", "https"} or not public.hostname or public.path or public.username):
        raise ValueError("Adres publiczny powinien mieć postać https://orchestrator.example.com (bez ścieżki).")
    runner = Runner(store, data_dir, **({"command_factory": command_factory} if command_factory else {}))
    lock = InstanceLock(data_dir / "instance.lock")
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        try:
            store.initialize()
            if not auth.has_users():
                raise RuntimeError("Najpierw utwórz konto: python manage.py add-user admin --role admin")
            store.recover()
            if start_worker:
                runner.start()
            yield
        finally:
            runner.stop()
            lock.release()

    app = FastAPI(title="Local Orchestrator", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.runner, app.state.token, app.state.auth = store, runner, token, auth
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", *([public.hostname] if public.hostname else [])])

    @app.middleware("http")
    async def local_api_guard(request: Request, call_next):
        user = auth.user(request.cookies.get(COOKIE))
        request.state.user = user
        if request.url.path.startswith("/api/") and request.url.path != "/api/login" and not user:
            return JSONResponse({"detail": "Zaloguj się, aby korzystać z panelu."}, status_code=401)
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("x-orchestrator-token", "")
            origin = request.headers.get("origin")
            allowed_origins = {str(request.base_url).rstrip("/")}
            if public_url:
                allowed_origins.add(public_url)
            if not hmac.compare_digest(supplied.encode(), token.encode()) or (origin and origin not in allowed_origins):
                return JSONResponse({"detail": "Odśwież panel, aby przywrócić połączenie z aplikacją."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(subprocess.TimeoutExpired)
    async def process_timeout(request, exc):
        return JSONResponse({"detail": "Git nie odpowiedział w wymaganym czasie."}, status_code=408)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not request.state.user:
            return RedirectResponse("/login", status_code=303)
        return (ROOT / "web" / "index.html").read_text(encoding="utf-8").replace("__API_TOKEN__", token)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if request.state.user:
            return RedirectResponse("/", status_code=303)
        return (ROOT / "web" / "login.html").read_text(encoding="utf-8").replace("__API_TOKEN__", token)

    @app.post("/api/login")
    def login(payload: LoginInput, request: Request):
        try:
            session = auth.login(payload.username, payload.password, request.client.host if request.client else "unknown")
        except ValueError as exc:
            raise HTTPException(429, str(exc)) from exc
        if not session:
            raise HTTPException(401, "Nieprawidłowy login lub hasło.")
        response = JSONResponse({"username": payload.username})
        response.set_cookie(COOKIE, session, max_age=SESSION_SECONDS, httponly=True,
                            secure=public.scheme == "https", samesite="strict")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, secure=public.scheme == "https", httponly=True, samesite="strict")
        return response

    @app.get("/api/health")
    def health(request: Request):
        return {"status": "ok", "providers": availability(), "git": bool(shutil.which("git")),
                "data_dir": str(data_dir), "parallel_jobs": 1, **request.state.user,
                "integrations": integration_status()}

    @app.get("/api/projects")
    def projects():
        return store.projects()

    @app.post("/api/projects", status_code=201)
    def add_project(payload: ProjectInput, request: Request):
        if request.state.user["role"] != "admin":
            raise HTTPException(403, "Dodawanie projektów wymaga roli administratora.")
        if not payload.name.strip():
            raise ValueError("Podaj nazwę projektu.")
        if bool(payload.path.strip()) == bool(payload.remote_url.strip()):
            raise ValueError("Podaj adres zdalnego repozytorium albo ścieżkę na serwerze.")
        if payload.remote_url:
            path = clone_remote(payload.remote_url.strip(), payload.branch, data_dir)
        else:
            try:
                path, _ = validate_project(payload.path.strip())
            except OSError as exc:
                raise ValueError("Nie można odczytać wskazanego katalogu.") from exc
        try:
            return store.add_project(payload.name.strip(), path, payload.instructions.strip(), payload.remote_url.strip(), payload.branch if payload.remote_url else "")
        except sqlite3.IntegrityError as exc:
            raise ValueError("To repozytorium jest już na liście projektów.") from exc

    @app.get("/api/jobs")
    def jobs():
        return store.jobs()

    def require_job(job_id):
        job = store.job(job_id)
        if not job:
            raise HTTPException(404, "Nie znaleziono zadania.")
        return job

    @app.post("/api/jobs", status_code=201)
    def add_job(payload: JobInput, request: Request):
        project = store.project(payload.project_id)
        if not project:
            raise HTTPException(404, "Nie znaleziono projektu.")
        if not payload.title.strip() or not payload.prompt.strip():
            raise ValueError("Podaj tytuł i treść zadania.")
        executable(payload.provider)
        base = project_commit(project)
        result = store.add_job(project["id"], payload.title.strip(), payload.prompt.strip(),
                               payload.provider, payload.model, payload.kind, payload.timeout_minutes, base,
                               created_by=request.state.user["username"])
        runner.wakeup.set()
        return result

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return require_job(job_id)

    @app.get("/api/jobs/{job_id}/events")
    def events(job_id: str, after: int = Query(default=0, ge=0)):
        require_job(job_id)
        return store.events(job_id, after)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        job = require_job(job_id)
        if request.state.user["role"] != "admin" and job["created_by"] != request.state.user["username"]:
            raise HTTPException(403, "Możesz anulować własne zadania. Administrator może anulować każde zadanie.")
        result = store.cancel(job_id)
        runner.wakeup.set()
        return result

    @app.post("/api/jobs/{job_id}/retry", status_code=201)
    def retry(job_id: str, request: Request):
        original = require_job(job_id)
        if original["status"] not in TERMINAL:
            raise HTTPException(409, "Poczekaj na zakończenie zadania lub anuluj je.")
        return add_job(JobInput(**{field: original[field] for field in
                                  ("project_id", "title", "prompt", "provider", "model", "kind", "timeout_minutes")}), request)

    @app.get("/api/jira/issues/{key}")
    def import_issue(key: str):
        return jira_issue(key)

    @app.get("/api/jobs/{job_id}/patch")
    def patch(job_id: str):
        require_job(job_id)
        path = data_dir / "workspaces" / job_id / "changes.patch"
        if not path.is_file():
            raise HTTPException(404, "Różnice nie są jeszcze gotowe.")
        return FileResponse(path, media_type="text/plain", filename=f"task-{job_id[:12]}.patch")

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    return app
