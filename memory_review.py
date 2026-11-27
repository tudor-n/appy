"""The laptop's side of memory review: keeps the overlay's card in sync with what the
consolidator proposed, applies Tudor's Accept / Deny / Edit and Undo, relays the
auto-approve setting, and announces every memory commit as a chip in the chat (and, via
`on_change`, to Appy himself on his next turn).

All calls to the memory service happen on this module's own thread, so the WebSocket
loop and the conversation never wait on the Mac.
"""
import json
import queue
import threading
from typing import Callable

POLL_SECONDS = 60


class MemoryReview:
    def __init__(self, client, emit_proposals: Callable[[list], None],
                 emit_committed: Callable[[str, str], None],
                 emit_settings: Callable[[bool], None] | None = None,
                 on_change: Callable[[str], None] | None = None,
                 poll_seconds: float = POLL_SECONDS):
        self.client = client
        self.emit_proposals = emit_proposals
        self.emit_committed = emit_committed
        self.emit_settings = emit_settings or (lambda on: None)
        self.on_change = on_change or (lambda line: None)
        self._head: str | None = None  # auto commits after this one haven't been announced
        self.poll_seconds = poll_seconds
        self._jobs: queue.Queue = queue.Queue()
        self._last: list | None = None  # what the overlay shows now
        self._errors: dict[str, str] = {}  # slug -> why its last decision failed
        self._thread = threading.Thread(target=self._loop, name="memory-review", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._jobs.put(("settings",))
        self.refresh()

    def stop(self) -> None:
        self._jobs.put(None)
        if self._thread.is_alive():
            self._thread.join(2)

    def refresh(self) -> None:
        self._jobs.put(("refresh",))

    def decide(self, slug: str, decision: str, text: str | None = None,
               before: str | None = None) -> None:
        """`before`: for an edit, the note as the card showed it when editing started."""
        self._jobs.put(("decide", slug, decision, text, before))

    def undo(self) -> None:
        self._jobs.put(("undo",))

    def set_auto_approve(self, on: bool) -> None:
        self._jobs.put(("auto", bool(on)))

    def tool_committed(self, name: str, result: str) -> None:
        """A chip for an explicit remember/forget that actually changed memory."""
        kinds = {"memory_remember": "remember", "memory_forget": "forget"}
        if name not in kinds:
            return
        try:
            data = json.loads(result)
        except ValueError:
            return
        if isinstance(data, dict) and data.get("commit") and data.get("line"):
            self.emit_committed(kinds[name], str(data["line"]))

    def _loop(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=self.poll_seconds)
            except queue.Empty:
                job = ("poll",)
            if job is None:
                return
            try:
                self._run(job)
            except Exception as exc:  # never let the review thread die
                print(f"[memory] review {job[0]} failed: {type(exc).__name__}: {exc}")

    def _run(self, job: tuple) -> None:
        kind = job[0]
        if kind in ("refresh", "poll"):
            self._sync(force=kind == "refresh")
            self._announce_auto()
        elif kind == "settings":
            self._send_settings()
        elif kind == "auto":
            result = self.client.set_settings(job[1])
            if "auto_approve" in result:
                print(f"[memory] auto-approve {'on' if result['auto_approve'] else 'off'}")
                self.emit_settings(bool(result["auto_approve"]))
            else:
                print(f"[memory] auto-approve not changed: {result.get('error')}")
                self._send_settings()  # so the pill shows what the Mac really has
            self._sync(force=True)
            self._announce_auto()
        elif kind == "decide":
            _, slug, decision, text, before = job
            self._decide(slug, decision, text, before)
        elif kind == "undo":
            result = self.client.undo()
            if result.get("undone"):
                print(f"[memory] undid: {result['undone']}")
                self.emit_committed("undo", str(result["undone"]))
                self.on_change(f"undid {str(result['undone']).removeprefix('memory: ')}")
            elif result.get("error"):
                print(f"[memory] undo failed: {result['error']}")
            self._sync(force=True)

    def _decide(self, slug: str, decision: str, text: str | None, before: str | None) -> None:
        result = self.client.decide(slug, decision, text, before)
        status = result.get("status")
        print(f"[memory] {decision} {slug}: {status or result.get('error')}")
        if status:
            self._errors.pop(slug, None)
        else:
            self._errors[slug] = str(result.get("error") or "that didn't work")
        if not result.get("commit"):
            pass  # nothing was written (denied, stale, or an edit identical to the note)
        elif status == "accepted":
            self.emit_committed("approved", str(result.get("summary", "")))
            self.on_change(f"saved to {slug}: {result.get('summary', '')}")
        elif status == "edited":
            self.emit_committed("edited", str(result.get("summary", "")))
            self.on_change(f"saved Tudor's edit to {slug}: {result.get('summary', '')}")
        self._sync(force=True)

    def _send_settings(self) -> None:
        settings = self.client.settings()
        if settings is not None:
            self.emit_settings(bool(settings["auto_approve"]))

    def _announce_auto(self) -> None:
        """Chips and live notes for commits the Mac auto-approved since the last check.
        The first check only takes the current head: older commits are history."""
        data = self.client.changes(self._head or "")
        if data is None:
            return
        if self._head is not None:
            for item in data["items"]:
                print(f"[memory] auto-saved {item['slug']}: {item['summary']}")
                self.emit_committed("auto", item["summary"])
                self.on_change(f"saved to {item['slug']}: {item['summary']}")
        if data["head"]:
            self._head = data["head"]

    def _sync(self, force: bool) -> None:
        """Send the card's list to the overlay if it changed (or `force`: e.g. so buttons
        disabled by a click come back even when the service couldn't be reached)."""
        items = self.client.proposals()
        if items is None:  # service down: keep showing what we had
            if not force or self._last is None:
                return
            items = [{k: v for k, v in i.items() if k != "error"} for i in self._last]
        live = {i["slug"] for i in items}
        self._errors = {s: e for s, e in self._errors.items() if s in live}
        for item in items:
            if item["slug"] in self._errors:
                item["error"] = self._errors[item["slug"]]
        if force or items != self._last:
            self._last = items
            self.emit_proposals(items)
