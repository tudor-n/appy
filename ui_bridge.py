import asyncio
import json
import threading
from typing import Callable, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn


class UiBridge:
    def __init__(self, on_user_text: Optional[Callable[[str], None]] = None):
        self.app = FastAPI()
        self.clients: set[WebSocket] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.on_user_text = on_user_text

        @self.app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket):
            await ws.accept()
            self.clients.add(ws)
            try:
                while True:
                    data = json.loads(await ws.receive_text())
                    if data.get("type") == "user_text" and self.on_user_text:
                        text = data.get("text", "").strip()
                        if text:
                            self.on_user_text(text)
            except WebSocketDisconnect:
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
        self.emit({"type": "status", "state": state, "label": label or state})

    def response(self, text: str, expression: str = "neutral"):
        self.emit({"type": "response", "text": text, "expression": expression})