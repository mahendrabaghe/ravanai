"""
RAVAN cloud API — FastAPI, stateless apart from in-memory sessions.

Endpoints
    GET  /health          liveness + mode (no auth, no secrets)
    POST /api/chat        one user turn, or tool results for the previous turn

The desktop app is untouched; this process imports only core/gemini.py,
core/prompt.txt and core/viseme.py from it. Nothing is written to disk, and
request text is never logged.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .ai import AIRejected, AIUnavailable, run_tool_continuation, run_user_turn
from .config import ROOT, VERSION, settings
from .sessions import store

WEB_DIR = ROOT / "web"

app = FastAPI(
    title="RAVAN AI",
    version=VERSION,
    docs_url=None,
    redoc_url=None,
)


def _origins() -> list[str]:
    if "*" in settings.allowed_origins:
        return ["*"]
    extra = ["https://localhost", "capacitor://localhost",
             "http://localhost:8000", "http://127.0.0.1:8000"]
    return list(dict.fromkeys(settings.allowed_origins + extra))


app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    max_age=600,
)


class _Limiter:
    """Per-address sliding window. In-memory on purpose: this is abuse
    damping for a personal endpoint, not billing enforcement."""

    def __init__(self, limit: int = 30, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque] = {}

    def allow(self, who: str) -> tuple[bool, float]:
        now = time.time()
        hits = self._hits.setdefault(who, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False, max(0.0, self.window - (now - hits[0]))
        hits.append(now)
        if len(self._hits) > 2000:
            for key in list(self._hits)[:500]:
                if not self._hits[key]:
                    self._hits.pop(key, None)
        return True, 0.0


limiter = _Limiter(limit=settings.rate_limit_per_min)


@app.middleware("http")
async def _guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        who = (request.client.host if request.client else "") or "unknown"
        ok, wait = limiter.allow(who)
        if not ok:
            return JSONResponse(
                {"detail": "Too many requests. Try again shortly."},
                status_code=429,
                headers={"Retry-After": str(int(wait) + 1)},
            )
    response = await call_next(request)
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def _auth(authorization: str | None = Header(default=None)) -> None:
    if not settings.api_token:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not secrets.compare_digest(supplied, settings.api_token):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")


class ChatBody(BaseModel):
    session_id: str = Field(min_length=4, max_length=64)
    request_id: str = Field(min_length=4, max_length=64)
    text: str | None = Field(default=None, max_length=4000)
    tool_results: list[dict] | None = None
    local_time: str | None = Field(default=None, max_length=80)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "ravan-ai",
        "version": VERSION,
        "mode": "mock" if settings.mock else "live",
        "ai_configured": settings.ai_configured,
        "auth_required": bool(settings.api_token),
        "sessions": await store.count(),
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@app.post("/api/chat", dependencies=[Depends(_auth)])
async def chat(body: ChatBody):
    text = (body.text or "").strip()
    has_results = bool(body.tool_results)
    if bool(text) == has_results:
        return JSONResponse(
            {"detail": "Send either text or tool_results, not both."},
            status_code=400,
        )
    if text and len(text) > settings.max_text:
        return JSONResponse({"detail": "Message is too long."}, status_code=400)

    session = await store.get(body.session_id)
    cached = session.cached(body.request_id)
    if cached is not None:
        return cached

    try:
        await asyncio.wait_for(session.lock.acquire(), timeout=0.8)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"detail": "The assistant is still answering the previous request."},
            status_code=409,
        )
    try:
        if has_results:
            payload = await run_tool_continuation(
                session, body.tool_results, body.local_time)
        else:
            payload = await run_user_turn(session, text, body.local_time)
        session.remember_reply(body.request_id, payload)
        return payload
    except AIUnavailable as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503)
    except AIRejected as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception as exc:
        print(f"[RAVAN] chat failed: {type(exc).__name__}: {str(exc)[:160]}")
        return JSONResponse(
            {"detail": "The assistant hit an unexpected error. Try again."},
            status_code=500,
        )
    finally:
        session.lock.release()


@app.get("/")
async def root():
    if WEB_DIR.exists():
        return RedirectResponse("/app/")
    return {"service": "ravan-ai", "version": VERSION,
            "health": "/health", "chat": "/api/chat"}


# The same static build GitHub Pages serves, mounted for local testing and for
# deployments that prefer one origin. Harmless on Render.
class _RevalidatingStatic(StaticFiles):
    """Serve the interface with revalidation.

    ES modules and the face JSON sit behind a CDN that otherwise caches them
    heuristically, so a deploy could keep running yesterday's renderer in a
    returning phone's WebView. `no-cache` still allows a conditional request
    and a 304 — it only stops a stale module being used without asking.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if WEB_DIR.exists():
    app.mount("/app", _RevalidatingStatic(directory=str(WEB_DIR), html=True),
              name="web")
