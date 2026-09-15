"""Authenticated web application with durable, resumable evidence-brief jobs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import secrets
import shutil
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from litreview.config import Config
from litreview.pipeline import brief, brief_flow
from litreview.web.runner import BriefWorker, ClaudeRunner, PicoDraft
from litreview.web.store import Store, write_json

STATIC = Path(__file__).parent / "static"
REPO = Path(__file__).resolve().parents[3]
COOKIE = "evidence_brief_session"
SESSION_SECONDS = 60 * 60 * 24 * 7


@dataclass
class Settings:
    root: Path
    password: str
    allowed_hosts: list[str]

    @classmethod
    def load(cls) -> Settings:
        root = Path(os.environ.get("EVIDENCE_BRIEF_DATA", str(REPO / ".evidence-brief"))).expanduser().resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_path = root / "access-key.txt"
        password = os.environ.get("EVIDENCE_BRIEF_PASSWORD", "")
        if not password:
            if not key_path.exists():
                with key_path.open("x", encoding="utf-8") as file:
                    key_path.chmod(0o600)
                    file.write(secrets.token_urlsafe(18) + "\n")
            password = key_path.read_text(encoding="utf-8").strip()
        if len(password) < 12:
            raise ValueError("EVIDENCE_BRIEF_PASSWORD must contain at least 12 characters")
        hosts = os.environ.get("EVIDENCE_BRIEF_HOSTS", "127.0.0.1,localhost").split(",")
        return cls(root, password, [host.strip() for host in hosts if host.strip()])


class NewBrief(BaseModel):
    question: str = Field(min_length=5, max_length=2000)


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class Addition(BaseModel):
    pico_id: str = Field(pattern=r"^pico_0[1-3]$")
    identifiers: list[str] = Field(default_factory=list, max_length=10)


class Checkpoint(BaseModel):
    note: str = Field(default="已確認納入研究。", max_length=2000)
    additions: list[Addition] = Field(default_factory=list, max_length=3)


def create_app(settings: Settings | None = None, cfg: Config | None = None,
               runner: ClaudeRunner | None = None) -> FastAPI:
    settings = settings or Settings.load()
    cfg = cfg or brief.brief_config()
    runner = runner or ClaudeRunner()
    store = Store(settings.root)
    worker = BriefWorker(store, cfg, runner)
    sessions: dict[str, float] = {}
    attempts: dict[str, deque[float]] = defaultdict(deque)
    pending: dict[str, asyncio.Task[None]] = {}
    gate = asyncio.Semaphore(1)
    capabilities = {"fulltext": False}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.recover()
        capabilities["fulltext"] = await asyncio.to_thread(brief_flow.fulltext_available, cfg)
        yield
        if pending:
            await asyncio.gather(*list(pending.values()), return_exceptions=True)

    app = FastAPI(title="Evidence Brief", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.state.store = store
    app.state.worker = worker
    app.state.pending = pending

    def authenticated(request: Request) -> bool:
        digest = hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()
        return sessions.get(digest, 0) > time.time()

    @app.middleware("http")
    async def protect(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.method == "POST":
            try:
                if int(request.headers.get("content-length", "0")) > 32768:
                    return JSONResponse({"detail": "輸入內容過長。"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "無效的請求。"}, status_code=400)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "請使用 JSON 格式送出。"}, status_code=415)
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if (origin and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "請從查詢頁面送出請求。"}, status_code=403)
            body = await request.body()
            if len(body) > 32768:
                return JSONResponse({"detail": "輸入內容過長。"}, status_code=413)
        public = request.url.path in {"/", "/evidence-brief", "/api/config", "/api/login", "/health"}
        if not public and not request.url.path.startswith("/static/") and not authenticated(request):
            return JSONResponse({"detail": "請先登入。"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        if "/report" in request.url.path:
            response.headers["Content-Security-Policy"] = (
                "sandbox allow-scripts allow-popups allow-modals; default-src 'none'; "
                "style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'")
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        messages = [error["msg"] for error in exc.errors()]
        return JSONResponse({"detail": "輸入資料需要調整：" + "；".join(messages)}, status_code=422)

    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return store.get(job_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, "找不到這筆查詢。") from exc

    def require_state(job_id: str, states: set[str]) -> dict[str, Any]:
        job = get_job(job_id)
        if job_id in pending or job["status"] not in states:
            raise HTTPException(409, "查詢狀態已更新，請重新整理。")
        return job

    async def run(job_id: str) -> None:
        try:
            async with gate:
                await asyncio.to_thread(worker.execute, job_id)
        finally:
            pending.pop(job_id, None)

    def enqueue(job_id: str) -> None:
        pending[job_id] = asyncio.create_task(run(job_id))

    def require_capacity() -> None:
        if len(pending) >= 4:
            raise HTTPException(429, "已有 4 筆查詢處理中，請稍後再試。")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    async def config(request: Request) -> dict[str, Any]:
        missing = []
        if not (cfg.pubmed_email or cfg.unpaywall_email):
            missing.append("PubMed 聯絡信箱尚未設定")
        if shutil.which(runner.executable) is None:
            missing.append("Claude Code 尚未安裝")
        return {"authenticated": authenticated(request), "configured": not missing,
                "missing": missing, "fulltext": capabilities["fulltext"]}

    @app.post("/api/login")
    async def login(payload: Login, request: Request, response: Response) -> dict[str, bool]:
        address = request.client.host if request.client else "unknown"
        history = attempts[address]
        timestamp = time.time()
        while history and history[0] < timestamp - 300:
            history.popleft()
        if len(history) >= 10:
            raise HTTPException(429, "登入嘗試過多，請在 5 分鐘後再試。")
        history.append(timestamp)
        if not secrets.compare_digest(payload.password.encode(), settings.password.encode()):
            raise HTTPException(401, "通行碼不正確。")
        history.clear()
        for digest, expires in list(sessions.items()):
            if expires <= timestamp:
                del sessions[digest]
        token = secrets.token_urlsafe(32)
        sessions[hashlib.sha256(token.encode()).hexdigest()] = timestamp + SESSION_SECONDS
        response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https",
                            samesite="strict", max_age=SESSION_SECONDS)
        return {"authenticated": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response) -> dict[str, bool]:
        sessions.pop(hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest(), None)
        response.delete_cookie(COOKIE)
        return {"authenticated": False}

    @app.get("/api/briefs")
    async def list_briefs() -> dict[str, Any]:
        fields = {"id", "question", "status", "message", "updated_at", "report_url"}
        return {"briefs": [{k: v for k, v in job.items() if k in fields} for job in store.list()]}

    @app.post("/api/briefs", status_code=202)
    async def create_brief(payload: NewBrief) -> dict[str, Any]:
        question = payload.question.strip()
        if len(question) < 5:
            raise HTTPException(422, "請輸入至少 5 個字的問題。")
        if not (cfg.pubmed_email or cfg.unpaywall_email):
            raise HTTPException(503, "伺服器尚未設定 PubMed 聯絡信箱。")
        require_capacity()
        job = store.create(question)
        enqueue(job["id"])
        return job

    @app.get("/api/briefs/{job_id}")
    async def detail(job_id: str) -> dict[str, Any]:
        return get_job(job_id)

    @app.post("/api/briefs/{job_id}/approve", status_code=202)
    async def approve(job_id: str, payload: PicoDraft) -> dict[str, Any]:
        job = require_state(job_id, {"pico_review"})
        require_capacity()
        data = {"question": job["question"], **payload.model_dump()}
        write_json(store.base(job_id) / "picos.json", data)
        updated = store.update(job_id, picos=data["picos"], preview=[], status="searching", phase="preview",
                               message="正在檢查 PubMed 搜尋詞…")
        enqueue(job_id)
        return updated

    @app.post("/api/briefs/{job_id}/checkpoint", status_code=202)
    async def checkpoint(job_id: str, payload: Checkpoint) -> dict[str, Any]:
        job = require_state(job_id, {"checkpoint"})
        require_capacity()
        pico_ids = {p["pico_id"] for p in job["picos"]}
        for addition in payload.additions:
            if addition.pico_id not in pico_ids or any(not s.strip() or len(s) > 250 for s in addition.identifiers):
                raise HTTPException(422, "請選擇有效 PICO 並輸入 PMID 或 DOI。")
        updated = store.update(job_id, checkpoint_payload=payload.model_dump(), phase="checkpoint",
                               status="running", message="已收到研究確認，正在繼續評讀…")
        enqueue(job_id)
        return updated

    @app.post("/api/briefs/{job_id}/resume", status_code=202)
    async def resume(job_id: str) -> dict[str, Any]:
        job = require_state(job_id, {"error", "interrupted"})
        require_capacity()
        status = "drafting" if job["phase"] == "draft" else "running"
        updated = store.update(job_id, status=status, message="已接續上次進度…")
        enqueue(job_id)
        return updated

    @app.get("/briefs/{job_id}/report")
    async def report(job_id: str) -> FileResponse:
        job = get_job(job_id)
        path = (store.base(job_id) / "brief.html").resolve()
        if job["status"] != "done" or not path.is_relative_to(store.base(job_id)) or not path.is_file():
            raise HTTPException(404, "摘要尚未完成。")
        return FileResponse(path, media_type="text/html; charset=utf-8")

    @app.get("/")
    @app.get("/evidence-brief")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Private Evidence Brief query page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    settings = Settings.load()
    logging.basicConfig(level=logging.INFO, filename=settings.root / "service.log",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print(f"Evidence Brief: http://{args.host}:{args.port}")
    print(f"Access key file: {settings.root / 'access-key.txt'}")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1,::1", access_log=False)


if __name__ == "__main__":
    main()
