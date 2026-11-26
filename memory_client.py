"""The laptop's side of long-term memory: HTTP calls to the memory service on the Mac.

Nothing here may break startup or a turn. When the service can't be reached,
`context()` returns None (Appy starts with prompt.txt alone), the tools return an error
the model can relay, and logs stay on disk to be uploaded at the next start.
"""
import fcntl
import json
import os
from pathlib import Path

import httpx

MEMORY_RULES = (
    "Use memory_read before answering about a person/project in the index. "
    "Only write memory when the user explicitly asks."
)
UNREACHABLE = json.dumps({"error": "long-term memory is unreachable right now"})
# The fields of a proposal as the overlay gets them (see PROTOCOL.md memory_proposals)
PROPOSAL_FIELDS = ("slug", "title", "summary", "before", "after", "stale")
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


def _in_use(path: Path) -> bool:
    """True if a running Appy still has this log open (SessionLogger holds a flock)."""
    try:
        with open(path, "rb") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        pass
    return False


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
        try:
            http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"},
                                timeout=TIMEOUT)
        except Exception as exc:  # a typo in .env must not stop Appy from booting
            print(f"[memory] off: bad MEMORY_URL or MEMORY_TOKEN ({type(exc).__name__}: {exc})")
            return None
        return cls(http, session_id)

    def context(self) -> dict | None:
        try:
            r = self.http.get("/context")
            r.raise_for_status()
            data = r.json()
            return {"core": str(data.get("core", "")), "index": str(data.get("index", ""))}
        except Exception as exc:  # never raise into startup
            print(f"[memory] unreachable ({type(exc).__name__}): starting without long-term memory")
            return None

    def activity(self) -> None:
        """Tell the Mac a turn just started, so the consolidator leaves the model alone."""
        try:
            self.http.post("/activity", timeout=2.0)
        except Exception:
            pass

    # -- tools: each returns a JSON string for the model ----------------------------

    def read(self, slug: str) -> str:
        return self._call("GET", f"/note/{slug}")

    def remember(self, text: str, slug: str | None = None) -> str:
        return self._call("POST", "/remember",
                          {"text": text, "slug": slug, "session": self.session_id})

    def forget(self, slug: str, text: str) -> str:
        return self._call("POST", "/forget", {"slug": slug, "text": text, "session": self.session_id})

    # -- review (memory_review.MemoryReview) ----------------------------------------

    def proposals(self) -> list[dict] | None:
        """What the consolidator proposed, for the overlay; None if it can't be reached."""
        try:
            r = self.http.get("/proposals")
            r.raise_for_status()
            return [{"slug": str(i["slug"]), "title": str(i.get("title") or i["slug"]),
                     "summary": str(i.get("summary", "")), "before": str(i.get("before", "")),
                     "after": str(i.get("new_text", "")), "stale": bool(i.get("stale"))}
                    for i in r.json()["items"]]
        except Exception as exc:
            print(f"[memory] proposals unavailable ({type(exc).__name__})")
            return None

    def decide(self, slug: str, decision: str, text: str | None = None,
               before: str | None = None) -> dict:
        body = {"decision": decision}
        if text is not None:
            body["text"] = text
        if before is not None:
            body["before"] = before
        return json.loads(self._call("POST", f"/proposals/{slug}", body))

    def undo(self) -> dict:
        return json.loads(self._call("POST", "/undo", {}))

    def _call(self, method: str, path: str, body: dict | None = None) -> str:
        try:
            r = self.http.request(method, path, json=body)
            data = r.json()
        except Exception as exc:  # e.g. httpx.InvalidURL for a slug with control characters
            print(f"[memory] {method} {path!r} failed: {type(exc).__name__}: {exc}")
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
        except Exception as exc:
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
            if path == Path(skip) or _in_use(path):
                continue
            if self.upload_log(path):  # a failed one stays on disk for the next start
                uploaded += 1
        if uploaded:
            print(f"[memory] uploaded {uploaded} earlier log(s)")
        return uploaded
