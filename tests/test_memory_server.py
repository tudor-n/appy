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


if __name__ == "__main__":
    unittest.main()
