import asyncio
import json
import threading
from typing import Callable, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn


VALID_EFFORTS = ("none", "low", "medium", "high")
MEMORY_DECISIONS = ("accept", "deny", "edit")
MEMORY_KINDS = ("remember", "forget", "approved", "edited", "undo", "auto")


class UiBridge:
    def __init__(
        self,
        on_user_text: Optional[Callable[[str], None]] = None,
        on_set_effort: Optional[Callable[[str], None]] = None,
        get_effort: Optional[Callable[[], str]] = None,
        get_mode: Optional[Callable[[], str]] = None,
        on_compact: Optional[Callable[[], None]] = None,
        on_memory_decision: Optional[Callable[[str, str, Optional[str], Optional[str]], None]] = None,
        on_memory_undo: Optional[Callable[[Optional[list]], None]] = None,
        on_memory_auto_approve: Optional[Callable[[bool], None]] = None,
    ):
        self.app = FastAPI()
        self.clients: set[WebSocket] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.on_user_text = on_user_text
        self.on_set_effort = on_set_effort
        self.get_effort = get_effort
        self.get_mode = get_mode
        self.on_compact = on_compact
        self.on_memory_decision = on_memory_decision
        self.on_memory_undo = on_memory_undo
        self.on_memory_auto_approve = on_memory_auto_approve
        # The auto-approve setting, replayed too (the panel pill's menu shows it)
        self.last_settings: Optional[dict] = None
        # The review card's list, replayed (like the status) to clients that connect later
        self.last_proposals: Optional[dict] = None
        # The last status sent, replayed to clients that connect later. The overlay is
        # usually still reconnecting when "booting" goes out, and would otherwise sit
        # looking idle through the whole model load.
        self.last_status: Optional[dict] = None
        # How full the context is (the input pill's compact ring), replayed the same way
        self.last_context: Optional[dict] = None

        @self.app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket):
            await ws.accept()
            self.clients.add(ws)
            try:
                # Sync the UI's effort picker with the backend's current value
                if self.get_effort:
                    await ws.send_json({"type": "effort", "value": self.get_effort()})
                # ...and the mascot's resting face with the current mode
                if self.get_mode:
                    await ws.send_json({"type": "mode", "value": self.get_mode()})
                if self.last_status:
                    await ws.send_json(self.last_status)
                if self.last_proposals:
                    await ws.send_json(self.last_proposals)
                if self.last_settings:
                    await ws.send_json(self.last_settings)
                if self.last_context:
                    await ws.send_json(self.last_context)
                while True:
                    raw = await ws.receive_text()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        print(f"[ui] ignoring malformed message: {raw[:80]!r}")
                        continue
                    if not isinstance(data, dict):
                        continue
                    kind = data.get("type")
                    if kind == "user_text" and self.on_user_text:
                        text = str(data.get("text", "")).strip()
                        if text:
                            self.on_user_text(text)
                    elif kind == "compact" and self.on_compact:
                        self.on_compact()
                    elif kind == "memory_decision" and self.on_memory_decision:
                        slug, decision = data.get("slug"), data.get("decision")
                        text = data.get("text")
                        if not isinstance(slug, str) or not slug or decision not in MEMORY_DECISIONS:
                            continue
                        if decision == "edit" and not (isinstance(text, str) and text.strip()):
                            continue
                        before = data.get("before")
                        if decision != "edit" or not isinstance(before, str):
                            before = None
                        self.on_memory_decision(slug, decision,
                                                text if decision == "edit" else None, before)
                    elif kind == "memory_undo" and self.on_memory_undo:
                        # `commits`: exactly the changes a pill's Undo means (newest first)
                        commits = data.get("commits")
                        if commits is None:
                            self.on_memory_undo(None)
                        elif (isinstance(commits, list) and commits
                              and all(isinstance(c, str) and c for c in commits)):
                            self.on_memory_undo(commits)
                    elif kind == "memory_auto_approve" and self.on_memory_auto_approve:
                        if isinstance(data.get("on"), bool):
                            self.on_memory_auto_approve(data["on"])
                    elif kind == "set_effort" and self.on_set_effort:
                        value = data.get("effort")
                        if value in VALID_EFFORTS:
                            self.on_set_effort(value)
            except WebSocketDisconnect:
                pass
            finally:
                self.clients.discard(ws)

    def start(self, host: str = "127.0.0.1", port: int = 8001):
        def _run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            config = uvicorn.Config(self.app, host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            self.loop.run_until_complete(server.serve())

        threading.Thread(target=_run, daemon=True).start()

    def emit(self, payload: dict):
        if not self.loop or not self.clients:
            return

        async def _send_all():
            dead = []
            for ws in list(self.clients):
                try:
                    await ws.send_json(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

        asyncio.run_coroutine_threadsafe(_send_all(), self.loop)

    def wake(self):
        self.emit({"type": "wake"})

    def transcript(self, text: str, is_final: bool = True):
        self.emit({"type": "transcript", "text": text, "is_final": is_final})

    def status(self, state: str, label: str = ""):
        self.last_status = {"type": "status", "state": state, "label": label or state}
        self.emit(self.last_status)

    def response(self, text: str, expression: str = "neutral"):
        self.emit({"type": "response", "text": text, "expression": expression})

    def context(self, tokens: int, limit: int):
        """How much of the context the conversation uses: `limit` is where it gets
        compacted automatically. The input pill's compact ring fills up towards it."""
        self.last_context = {"type": "context", "tokens": max(0, int(tokens)),
                             "limit": max(1, int(limit))}
        self.emit(self.last_context)

    def effort(self, value: str):
        self.emit({"type": "effort", "value": value})

    def mode(self, value: str):
        """Appy's mode ("banter" | "focus"): the overlay rests on the focused face in focus."""
        self.emit({"type": "mode", "value": value})

    def memory_proposals(self, items: list):
        """The whole review list (empty = nothing to review); see PROTOCOL.md."""
        self.last_proposals = {"type": "memory_proposals", "items": items}
        self.emit(self.last_proposals)

    def memory_settings(self, auto_approve: bool):
        self.last_settings = {"type": "memory_settings", "auto_approve": bool(auto_approve)}
        self.emit(self.last_settings)

    def memory_committed(self, kind: str, summary: str, facts=(), dropped=(), commit: str = ""):
        """`facts`/`dropped`: the sentences the change added and removed (the overlay's
        memory pill lists them); `commit`: so the pill's Undo can name exactly this one."""
        self.emit({"type": "memory_committed", "kind": kind, "summary": summary,
                   "facts": list(facts), "dropped": list(dropped), "commit": commit or ""})

    def memory_error(self, text: str):
        """A memory action failed (e.g. an undo): shown in the chat."""
        self.emit({"type": "memory_error", "text": text})

    def widget(self, card: dict):
        """Show a card next to Appy; `card` carries its own "widget" kind (see PROTOCOL.md)."""
        self.emit({"type": "widget", **card})