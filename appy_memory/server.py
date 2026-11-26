"""The memory service on the Mac mini: HTTP in front of the vault.

Run with `python -m appy_memory.server`, with these settings in the environment or
in ./.env: MEMORY_TOKEN (required), MEMORY_DIR (default ~/appy-data),
MEMORY_HOST (default 0.0.0.0) and MEMORY_PORT (default 8765). For the consolidator:
BASE_URL and MODEL (LM Studio), optional API_KEY, CONSOLIDATOR_EFFORT (low),
CONSOLIDATOR_WINDOW_TOKENS (5000), CONSOLIDATOR_IDLE_SECONDS (120) and
CONSOLIDATOR_DUMP=1 to write every prompt and output to MEMORY_DIR/dumps/.

Every endpoint needs `Authorization: Bearer <MEMORY_TOKEN>`. All vault calls go through
one lock, so there's only ever one writer.
"""
from __future__ import annotations

import hmac
import os
import threading
import time
from pathlib import Path
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .consolidator import Consolidator, Worker
from .llm import LLM
from .vault import NoteFormatError, Vault, VaultError, check_note


class RememberBody(BaseModel):
    text: str
    slug: Optional[str] = None
    session: str = ""


class DecisionBody(BaseModel):
    decision: str
    text: Optional[str] = None
    # Edit only: the note as the card showed it when editing started
    before: Optional[str] = None


class ForgetBody(BaseModel):
    slug: str
    text: str
    session: str = ""


def _say(line: str) -> None:
    print(f"[memory] {line}", flush=True)  # launchd captures stdout into service.log


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def create_app(vault: Vault, token: str, worker=None) -> FastAPI:
    """`worker` (a consolidator Worker, also settable later as app.state.worker) is kicked
    whenever a new session log arrives."""
    app = FastAPI(title="Appy memory")
    lock = threading.Lock()
    app.state.vault = vault
    app.state.lock = lock  # the consolidator takes it for its vault reads and writes
    app.state.worker = worker
    app.state.last_activity = 0.0  # time.time() of the laptop's last turn start
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
        if stored and app.state.worker is not None:
            app.state.worker.kick()
        return {"stored": stored}

    @app.post("/activity", dependencies=guarded)
    def activity():
        app.state.last_activity = time.time()  # the consolidator yields to live chat
        return {"ok": True}

    @app.get("/proposals", dependencies=guarded)
    def proposals():
        return {"items": locked(vault.proposals)}

    @app.post("/proposals/{slug}", dependencies=guarded)
    def decide(slug: str, body: DecisionBody):
        """Tudor's Accept / Deny / Edit from the overlay. Only this code writes the note."""
        if body.decision not in ("accept", "deny", "edit"):
            raise VaultError(422, f"unknown decision {body.decision!r}")
        with lock:
            item = vault.proposal(slug)
            if item is None:
                raise VaultError(404, f"there's no pending update for '{slug}'")
            sessions = f"sessions={','.join(item.get('sessions', []))}"
            summary = item.get("summary", "")
            if body.decision == "deny":
                vault.delete_proposal(slug)
                _say(f"proposal {slug}: denied")
                return {"status": "denied"}
            if body.decision == "edit":
                if body.before is not None and body.before != (vault.note_text(slug) or ""):
                    raise VaultError(409, f"'{slug}' changed while you were editing; "
                                          "your text wasn't saved")
                try:
                    text = check_note(slug, body.text or "", vault.note_text(slug))
                except NoteFormatError as exc:
                    raise VaultError(400, f"edit not saved: {exc.reason}")
                sha = vault.apply_note(slug, text, f"memory: {summary[:60]} [edited]", sessions)
                _say(f"proposal {slug}: edited -> {sha}")
                return {"status": "edited", "commit": sha, "summary": summary}
            if vault.note_hash(slug) != item.get("base_hash"):
                # The note changed since this was written: redo it against the new note
                if app.state.worker is None:
                    raise VaultError(409, f"'{slug}' changed since this update was written, "
                                          "and the consolidator is off: deny or edit it")
                vault.mark_stale(slug)
                app.state.worker.refresh(slug)
                _say(f"proposal {slug}: stale, redoing it")
                return {"status": "stale"}
            sha = vault.apply_note(slug, item["new_text"], f"memory: {summary[:60]} [approved]",
                                   sessions)
            _say(f"proposal {slug}: accepted -> {sha}")
            return {"status": "accepted", "commit": sha, "summary": summary}

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
    app = create_app(vault, token)
    _say(f"vault {root}, listening on {host}:{port}")
    base_url = os.getenv("BASE_URL", "").strip()
    if base_url:
        model = os.getenv("MODEL") or "google/gemma-4-26b-a4b"
        llm = LLM(base_url, model, api_key=os.getenv("API_KEY", "").strip(),
                  effort=os.getenv("CONSOLIDATOR_EFFORT") or "none")
        dumps = root / "dumps" if os.getenv("CONSOLIDATOR_DUMP") else None
        consolidator = Consolidator(
            vault, llm, app.state.lock,
            window_tokens=int(os.getenv("CONSOLIDATOR_WINDOW_TOKENS") or "5000"),
            idle_seconds=float(os.getenv("CONSOLIDATOR_IDLE_SECONDS") or "120"),
            last_activity=lambda: app.state.last_activity, dump_dir=dumps)
        app.state.worker = Worker(consolidator)
        app.state.worker.start()
        app.state.worker.kick()  # sessions uploaded while the service was down
        print(f"[consolidator] on: {model} at {base_url}, effort {llm.effort}"
              f"{f', dumps in {dumps}' if dumps else ''}", flush=True)
    else:
        print("[consolidator] off (BASE_URL not set)", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
