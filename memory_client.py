"""The laptop's side of long-term memory: HTTP calls to the memory service on the Mac.

Nothing here may break startup or a turn. When the service can't be reached,
`context()` returns None (Appy starts with prompt.txt alone), the tools return an error
the model can relay, and logs stay on disk to be uploaded at the next start.
"""
import json
import os
from pathlib import Path

import httpx

MEMORY_RULES = (
    "Use memory_read before answering about a person/project in the index. "
    "Only write memory when the user explicitly asks."
)
UNREACHABLE = json.dumps({"error": "long-term memory is unreachable right now"})
TIMEOUT = 3.0
UPLOAD_TIMEOUT = 5.0


def system_prompt(base: str, context: dict | None) -> str:
    """prompt.txt plus core.md and the note index. Built once per run (KV cache)."""
    if context is None:
        return base
    core = (context.get("core") or "").strip() or "(empty)"
    index = (context.get("index") or "").strip() or "(none yet)"
    return (f"{base}\n\n[Long-term memory]\n{core}\n\n[Notes you can read]\n{index}"
            f"\n\n{MEMORY_RULES}")


class MemoryClient:
    def __init__(self, http: httpx.Client, session_id: str):
        self.http = http
        self.session_id = session_id

    @classmethod
    def from_env(cls, session_id: str) -> "MemoryClient | None":
        url = os.getenv("MEMORY_URL", "").strip()
        if not url:
            return None
        token = os.getenv("MEMORY_TOKEN", "").strip()
        http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"},
                            timeout=TIMEOUT)
        return cls(http, session_id)

    def context(self) -> dict | None:
        try:
            r = self.http.get("/context")
            r.raise_for_status()
            data = r.json()
            return {"core": str(data.get("core", "")), "index": str(data.get("index", ""))}
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            print(f"[memory] unreachable ({type(exc).__name__}): starting without long-term memory")
            return None

    # -- tools: each returns a JSON string for the model ----------------------------

    def read(self, slug: str) -> str:
        return self._call("GET", f"/note/{slug}")

    def remember(self, text: str, slug: str | None = None) -> str:
        return self._call("POST", "/remember",
                          {"text": text, "slug": slug, "session": self.session_id})

    def forget(self, slug: str, text: str) -> str:
        return self._call("POST", "/forget", {"slug": slug, "text": text, "session": self.session_id})

    def _call(self, method: str, path: str, body: dict | None = None) -> str:
        try:
            r = self.http.request(method, path, json=body)
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[memory] {method} {path} failed: {type(exc).__name__}: {exc}")
            return UNREACHABLE
        if r.status_code >= 500 and "error" not in data:
            return UNREACHABLE
        return json.dumps(data, ensure_ascii=False)

    # -- session logs ---------------------------------------------------------------

    def upload_log(self, path: Path) -> bool:
        """Send a finished log to the Mac and delete it here once it's stored there."""
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError:
            return False
        try:
            r = self.http.post(f"/logs/{path.stem}", content=data, timeout=UPLOAD_TIMEOUT)
        except httpx.HTTPError as exc:
            print(f"[memory] log {path.name} not uploaded ({type(exc).__name__})")
            return False
        if not r.is_success:
            print(f"[memory] log {path.name} not uploaded ({r.status_code}: {r.text[:120]})")
            return False
        try:
            path.unlink()
        except OSError as exc:
            print(f"[memory] uploaded {path.name} but couldn't delete it: {exc}")
        return True

    def upload_leftovers(self, logs_dir: Path, skip: Path) -> int:
        """Upload logs left behind by earlier runs (a crash, or the Mac was down)."""
        uploaded = 0
        try:
            paths = sorted(Path(logs_dir).glob("*.jsonl"))
        except OSError:
            return 0
        for path in paths:
            if path == Path(skip):
                continue
            if self.upload_log(path):  # a failed one stays on disk for the next start
                uploaded += 1
        if uploaded:
            print(f"[memory] uploaded {uploaded} earlier log(s)")
        return uploaded
