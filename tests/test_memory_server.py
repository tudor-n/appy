"""Tests for the memory service's HTTP layer (runs on the Mac's Python 3.9 too).

Don't import tests/fakes.py here: it uses `str | None` at runtime, which 3.9 rejects.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from appy_memory import server
from appy_memory.server import create_app
from appy_memory.vault import Vault

TOKEN = "t0k"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SID = "2026-11-26_153012"


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.app = create_app(Vault(self.root).open(), TOKEN)
        self.http = TestClient(self.app, headers=AUTH)

    def tearDown(self):
        self._tmp.cleanup()

    def remember(self, text: str, slug=None, http=None):
        return (http or self.http).post("/remember",
                                        json={"text": text, "slug": slug, "session": SID})

    def test_requires_token(self):
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}):
            anon = TestClient(self.app, headers=headers)
            self.assertEqual(anon.get("/context").status_code, 401)
            self.assertEqual(anon.post("/remember", json={"text": "x", "session": SID}).status_code,
                             401)
            self.assertEqual(anon.post(f"/logs/{SID}", content=b"{}\n").status_code, 401)
        self.assertEqual(self.http.get("/context").status_code, 200)

    def test_remember_then_read_and_context(self):
        r = self.remember("Tudor's printer is a Prusa MK4.", "lab-printer")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["line"], "- Tudor's printer is a Prusa MK4.")
        note = self.http.get("/note/lab-printer").json()
        self.assertEqual(note["slug"], "lab-printer")
        self.assertIn("- Tudor's printer is a Prusa MK4.", note["text"])
        self.assertIn("lab-printer | Lab printer", self.http.get("/context").json()["index"])
        self.assertEqual(self.http.get("/note/nope").status_code, 404)

    def test_errors_are_json(self):
        r = self.remember("fact", "Bad/Slug")
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())
        self.remember("The printer is a Prusa.", "lab")
        self.remember("The printer sits by the window.", "lab")
        r = self.http.post("/forget", json={"slug": "lab", "text": "printer", "session": SID})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["lines"],
                         ["- The printer is a Prusa.", "- The printer sits by the window."])
        self.assertEqual(self.http.post("/remember", json={"slug": "x"}).status_code, 422)

    def test_upload_is_idempotent(self):
        body = b'{"type":"session_start"}\n'
        first = self.http.post(f"/logs/{SID}", content=body)
        self.assertEqual((first.status_code, first.json()), (200, {"stored": True}))
        self.assertEqual(self.http.post(f"/logs/{SID}", content=body).json(), {"stored": False})
        self.assertEqual(self.http.post(f"/logs/{SID}", content=body + b"{}\n").status_code, 409)

    def test_undo_endpoint(self):
        self.remember("Tudor lives in Sibiu.")
        r = self.http.post("/undo", json={})
        self.assertTrue(r.json()["undone"].startswith("memory: remember core:"))
        self.assertEqual(self.http.post("/undo", json={}).json(), {"undone": None, "commit": None})

    def test_writes_are_serialised(self):
        results = []

        def worker(i):
            # TestClient isn't thread-safe: one per thread, all over the same app
            results.append(self.remember(f"Fact {i}.", http=TestClient(self.app, headers=AUTH)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([r.status_code for r in results], [200] * 8)
        core = (self.root / "memory" / "core.md").read_text()
        self.assertEqual(core.count("- Fact "), 8)
        count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=self.root,
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(count, "9")  # init + 8

    def test_activity_and_proposals_endpoints(self):
        self.assertEqual(self.app.state.last_activity, 0.0)
        self.assertEqual(self.http.post("/activity").json(), {"ok": True})
        self.assertGreater(self.app.state.last_activity, 0)
        self.assertEqual(self.http.get("/proposals").json(), {"items": []})
        self.app.state.vault.save_proposal("lab", "Lab", "# Lab\n> g\n- a\n", "added a", SID, 0)
        [item] = self.http.get("/proposals").json()["items"]
        self.assertEqual((item["slug"], item["summary"]), ("lab", "added a"))
        anon = TestClient(self.app)
        self.assertEqual(anon.post("/activity").status_code, 401)
        self.assertEqual(anon.get("/proposals").status_code, 401)

    def test_upload_kicks_worker(self):
        kicks = []
        self.app.state.worker = type("W", (), {"kick": lambda self: kicks.append(1)})()
        body = b'{"type":"session_start"}\n'
        self.http.post(f"/logs/{SID}", content=body)
        self.http.post(f"/logs/{SID}", content=body)  # same bytes again: nothing new
        self.assertEqual(kicks, [1])

    def test_main_refuses_without_token(self):
        env = {k: v for k, v in os.environ.items() if k != "MEMORY_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(server, "load_dotenv", lambda *a, **k: None), \
                mock.patch.object(server.uvicorn, "run") as run:
            with self.assertRaises(SystemExit):
                server.main()
        run.assert_not_called()


NOTE = "# Bench\n> Lab bench gear\n- The PSU is a Riden.\n"
NEW = "# Bench\n> Lab bench gear\n- The PSU is a Korad.\n"


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = Vault(self.root).open()
        self.vault.remember("The PSU is a Riden.", "bench", SID)
        (self.root / "memory" / "bench.md").write_text(NOTE)
        self.vault.git.commit("memory: seed bench")
        self.refreshed = []
        worker = type("W", (), {"kick": lambda w: None,
                                "refresh": lambda w, slug: self.refreshed.append(slug)})()
        self.app = create_app(self.vault, TOKEN, worker=worker)
        self.http = TestClient(self.app, headers=AUTH)
        self.vault.save_proposal("bench", "Bench", NEW, "Replaced the PSU.", "2026-11-27_001310", 0)

    def tearDown(self):
        self._tmp.cleanup()

    def decide(self, decision, slug="bench", text=None):
        body = {"decision": decision}
        if text is not None:
            body["text"] = text
        return self.http.post(f"/proposals/{slug}", json=body)

    def git(self, *args) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True).stdout

    def test_accept_commits_and_deletes(self):
        r = self.decide("accept")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["status"], r.json()["summary"]), ("accepted", "Replaced the PSU."))
        self.assertEqual((self.root / "memory" / "bench.md").read_text(), NEW)
        self.assertIsNone(self.vault.proposal("bench"))
        subject, body = self.git("log", "-1", "--format=%s%x1f%b").split("\x1f")
        self.assertEqual(subject, "memory: Replaced the PSU. [approved]")
        self.assertIn("sessions=2026-11-27_001310", body)
        self.assertIn("bench | Bench", (self.root / "memory" / "_index.md").read_text())

    def test_accept_new_note(self):
        self.vault.save_proposal("lumen", "Lumen", "# Lumen\n> BI app\n- Uses DuckDB.\n", "New note.", SID, 0)
        self.assertEqual(self.decide("accept", "lumen").json()["status"], "accepted")
        self.assertIn("DuckDB", (self.root / "memory" / "lumen.md").read_text())

    def test_accept_stale_base_queues_rewrite(self):
        self.vault.remember("The DMM is a Fluke 87V.", "bench", "live")
        before = (self.root / "memory" / "bench.md").read_text()
        r = self.decide("accept")
        self.assertEqual(r.json(), {"status": "stale"})
        self.assertEqual((self.root / "memory" / "bench.md").read_text(), before)
        self.assertTrue(self.vault.proposal("bench")["stale"])
        self.assertEqual(self.refreshed, ["bench"])

    def test_accept_stale_without_consolidator_is_refused(self):
        app = create_app(self.vault, TOKEN)  # BASE_URL unset: nobody can redo it
        self.vault.remember("The DMM is a Fluke 87V.", "bench", "live")
        r = TestClient(app, headers=AUTH).post("/proposals/bench", json={"decision": "accept"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("changed", r.json()["error"])

    def test_deny_deletes(self):
        self.assertEqual(self.decide("deny").json(), {"status": "denied"})
        self.assertIsNone(self.vault.proposal("bench"))
        self.assertEqual((self.root / "memory" / "bench.md").read_text(), NOTE)

    def test_edit_commits_text(self):
        text = "# Bench\n> Lab bench gear\n- The PSU is a Korad KA3005P.\n"
        r = self.decide("edit", text=text)
        self.assertEqual(r.json()["status"], "edited")
        self.assertEqual((self.root / "memory" / "bench.md").read_text(), text)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(),
                         "memory: Replaced the PSU. [edited]")
        self.assertIsNone(self.vault.proposal("bench"))

    def test_edit_validates(self):
        for text, reason in (("- no title\n", "title"), ("# Renamed\n> g\n- x\n", "renamed"),
                             ("# Bench\n> g\nprose line\n", "bullet"),
                             ("# Bench\n> g\n" + "- fact that is long enough\n" * 200, "cap"),
                             ("", "empty")):
            with self.subTest(reason=reason):
                r = self.decide("edit", text=text)
                self.assertEqual(r.status_code, 400)
                self.assertIn("error", r.json())
        self.assertEqual((self.root / "memory" / "bench.md").read_text(), NOTE)
        self.assertIsNotNone(self.vault.proposal("bench"))

    def test_decision_on_missing_proposal_404(self):
        for decision in ("accept", "deny"):
            self.assertEqual(self.decide(decision, slug="nope").status_code, 404)
        self.assertEqual(self.decide("edit", slug="nope", text=NEW).status_code, 404)
        self.assertEqual(self.decide("maybe").status_code, 422)

    def test_proposals_include_before_and_stale(self):
        [item] = self.http.get("/proposals").json()["items"]
        self.assertEqual((item["before"], item["new_text"], item["stale"]), (NOTE, NEW, False))

    def test_undo_reverts_approved(self):
        self.decide("accept")
        r = self.http.post("/undo", json={})
        self.assertEqual(r.json()["undone"], "memory: Replaced the PSU. [approved]")
        self.assertEqual((self.root / "memory" / "bench.md").read_text(), NOTE)


if __name__ == "__main__":
    unittest.main()
