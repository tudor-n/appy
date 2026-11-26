"""Append-only JSONL record of one run of Appy: the raw source of truth for memory.

One JSON object per line, flushed after every write, so a crash (even kill -9) leaves
a valid file with everything up to the last event. Logging must never break a turn:
the first write error disables the logger and later calls do nothing.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

LOG_VERSION = 1
MAX_TOOL_RESULT_CHARS = 4096


def default_logs_dir() -> Path:
    return Path(os.getenv("APPY_LOG_DIR") or "~/.local/share/appy/logs").expanduser()


class SessionLogger:
    def __init__(
        self,
        logs_dir: Path,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self._now = now
        self.session_id = now().strftime("%Y-%m-%d_%H%M%S")
        self.path = Path(logs_dir) / f"{self.session_id}.jsonl"
        self._lock = threading.Lock()
        self._file = None
        self._seq = 0
        self._disabled = False

    def start(self, model: str, effort: str) -> None:
        self._write("session_start", model=model, effort=effort)

    def user(self, text: str, source: str) -> None:
        self._write("user", text=text, source=source)

    def assistant(self, text: str, expression: str) -> None:
        self._write("assistant", text=text, expression=expression)

    def tool_call(self, call_id: str, name: str, args: str) -> None:
        self._write("tool_call", call_id=call_id, name=name, args=args)

    def tool_result(self, call_id: str, name: str, result: str) -> None:
        extra = {}
        if len(result) > MAX_TOOL_RESULT_CHARS:
            result = result[:MAX_TOOL_RESULT_CHARS]
            extra["truncated"] = True
        self._write("tool_result", call_id=call_id, name=name, result=result, **extra)

    def compaction(self, summary: str) -> None:
        self._write("compaction", summary=summary)

    def end(self, reason: str) -> None:
        self._write("session_end", reason=reason)
        with self._lock:
            if self._file:
                try:
                    os.fsync(self._file.fileno())
                    self._file.close()
                except OSError as exc:
                    self._disable(exc)
                self._file = None

    def _write(self, kind: str, **fields) -> None:
        with self._lock:
            if self._disabled:
                return
            self._seq += 1
            event = {"v": LOG_VERSION, "seq": self._seq, "ts": self._now().isoformat(),
                     "type": kind, **fields}
            try:
                if self._file is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._file = open(self.path, "a", encoding="utf-8")
                self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._file.flush()
            except OSError as exc:
                self._disable(exc)

    def _disable(self, exc: OSError) -> None:
        self._disabled = True
        print(f"[log] disabled: {exc}")


class NullLogger:
    """Same interface as SessionLogger; records nothing."""

    def start(self, model: str, effort: str) -> None: ...
    def user(self, text: str, source: str) -> None: ...
    def assistant(self, text: str, expression: str) -> None: ...
    def tool_call(self, call_id: str, name: str, args: str) -> None: ...
    def tool_result(self, call_id: str, name: str, result: str) -> None: ...
    def compaction(self, summary: str) -> None: ...
    def end(self, reason: str) -> None: ...
