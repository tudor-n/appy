"""The memory service on the Mac mini: HTTP in front of the vault.

Run with `python -m appy_memory.server`, with these settings in the environment or
in ./.env: MEMORY_TOKEN (required), MEMORY_DIR (default ~/appy-data),
MEMORY_HOST (default 0.0.0.0) and MEMORY_PORT (default 8765).

Every endpoint needs `Authorization: Bearer <MEMORY_TOKEN>`. All vault calls go through
one lock, so there's only ever one writer.
"""
from __future__ import annotations

import hmac
import os
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .vault import Vault, VaultError


class RememberBody(BaseModel):
    text: str
    slug: Optional[str] = None
    session: str = ""


class ForgetBody(BaseModel):
    slug: str
    text: str
    session: str = ""


def _say(line: str) -> None:
    print(f"[memory] {line}", flush=True)  # launchd captures stdout into service.log


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def create_app(vault: Vault, token: str) -> FastAPI:
    app = FastAPI(title="Appy memory")
    lock = threading.Lock()
    expected = f"Bearer {token}".encode()

    def locked(fn, *args):
        with lock:
            return fn(*args)

    def auth(request: Request) -> None:
        given = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(given, expected):
            raise VaultError(401, "missing or wrong memory token")

    @app.exception_handler(VaultError)
    async def vault_error(request: Request, exc: VaultError):
        if exc.status != 401:
            _say(f"{request.method} {request.url.path} -> {exc.status}: {exc.message}")
        return JSONResponse({"error": exc.message, **exc.extra}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": f"bad request: {exc.errors()}"}, status_code=422)

    guarded = [Depends(auth)]

    @app.get("/context", dependencies=guarded)
    def context():
        return locked(vault.context)

    @app.get("/note/{slug}", dependencies=guarded)
    def note(slug: str):
        return {"slug": slug, "text": locked(vault.read, slug)}

    @app.post("/remember", dependencies=guarded)
    def remember(body: RememberBody):
        result = locked(vault.remember, body.text, body.slug, body.session)
        _say(f'remember {result["slug"]} "{_short(result["line"][2:])}" -> '
             f'{result["commit"] or "already there"} (session {body.session})')
        return result

    @app.post("/forget", dependencies=guarded)
    def forget(body: ForgetBody):
        result = locked(vault.forget, body.slug, body.text, body.session)
        _say(f'forget {result["slug"]} "{_short(result["line"][2:])}" -> {result["commit"]} '
             f"(session {body.session})")
        return result

    @app.post("/undo", dependencies=guarded)
    def undo():
        result = locked(vault.undo)
        _say(f'undo -> {result["commit"]}: {result["undone"]}' if result["undone"]
             else "undo -> nothing to undo")
        return result

    @app.post("/logs/{session_id}", dependencies=guarded)
    async def upload_log(session_id: str, request: Request):
        data = await request.body()
        stored = await run_in_threadpool(locked, vault.store_log, session_id, data)
        _say(f"log {session_id}: {len(data)} bytes, {'new' if stored else 'same'}")
        return {"stored": stored}

    return app


def main() -> None:
    load_dotenv()
    token = os.getenv("MEMORY_TOKEN", "").strip()
    if not token:
        raise SystemExit("MEMORY_TOKEN is not set (put it in .env next to the service)")
    root = Path(os.getenv("MEMORY_DIR") or "~/appy-data").expanduser()
    vault = Vault(root).open()
    host = os.getenv("MEMORY_HOST") or "0.0.0.0"
    port = int(os.getenv("MEMORY_PORT") or "8765")
    _say(f"vault {root}, listening on {host}:{port}")
    uvicorn.run(create_app(vault, token), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
