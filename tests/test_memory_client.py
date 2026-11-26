import json
import os
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from appy_memory.server import create_app
from appy_memory.vault import Vault
from memory_client import MEMORY_RULES, MemoryClient, system_prompt

SID = "2026-11-26_153012"
UNREACHABLE = {"error": "long-term memory is unreachable right now"}


def down_client() -> MemoryClient:
    return MemoryClient(httpx.Client(base_url="http://127.0.0.1:9", timeout=0.5), SID)


class MemoryClientTests(unittest.TestCase):
    def setUp(self):
        # upload_log passes a per-request timeout, which TestClient warns about
        warnings.filterwarnings("ignore", message="You should not use the 'timeout'")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vault_dir = self.tmp / "vault"
        app = create_app(Vault(self.vault_dir).open(), "t")
        self.memory = MemoryClient(TestClient(app, headers={"Authorization": "Bearer t"}), SID)
        self.logs = self.tmp / "logs"
        self.logs.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def log_file(self, name: str, body: bytes = b'{"type":"session_start"}\n') -> Path:
        path = self.logs / name
        path.write_bytes(body)
        return path

    def test_remember_read_forget_roundtrip(self):
        saved = json.loads(self.memory.remember("Tudor's printer is a Prusa MK4.", "lab-printer"))
        self.assertEqual(saved["line"], "- Tudor's printer is a Prusa MK4.")
        body = subprocess.run(["git", "log", "-1", "--format=%b"], cwd=self.vault_dir,
                              capture_output=True, text=True).stdout
        self.assertIn(f"session={SID}", body)
        self.assertIn("Prusa MK4", json.loads(self.memory.read("lab-printer"))["text"])
        context = self.memory.context()
        self.assertIn("lab-printer", context["index"])
        forgot = json.loads(self.memory.forget("lab-printer", "prusa"))
        self.assertEqual(forgot["line"], "- Tudor's printer is a Prusa MK4.")
        core = json.loads(self.memory.remember("Tudor lives in Sibiu."))
        self.assertEqual(core["slug"], "core")

    def test_service_errors_reach_the_model(self):
        self.assertIn("error", json.loads(self.memory.read("Bad")))
        self.assertIn("error", json.loads(self.memory.forget("core", "nothing like this")))

    def test_service_down_context_is_none(self):
        self.assertIsNone(down_client().context())

    def test_service_down_tools_return_error_json(self):
        memory = down_client()
        self.assertEqual(json.loads(memory.read("core")), UNREACHABLE)
        self.assertEqual(json.loads(memory.remember("x")), UNREACHABLE)
        self.assertEqual(json.loads(memory.forget("core", "x")), UNREACHABLE)

    def test_service_down_upload_keeps_file(self):
        path = self.log_file(f"{SID}.jsonl")
        self.assertFalse(down_client().upload_log(path))
        self.assertTrue(path.exists())

    def test_upload_deletes_on_success_and_is_idempotent(self):
        path = self.log_file(f"{SID}.jsonl")
        self.assertTrue(self.memory.upload_log(path))
        self.assertFalse(path.exists())
        self.log_file(f"{SID}.jsonl")  # a crash between upload and delete left it behind
        self.assertTrue(self.memory.upload_log(path))
        self.assertFalse(path.exists())
        self.assertFalse(self.memory.upload_log(path))  # gone: nothing to do

    def test_upload_conflict_keeps_file(self):
        self.memory.upload_log(self.log_file(f"{SID}.jsonl"))
        path = self.log_file(f"{SID}.jsonl", b'{"type":"other"}\n')
        self.assertFalse(self.memory.upload_log(path))
        self.assertTrue(path.exists())

    def test_upload_leftovers_skips_current(self):
        self.log_file("2026-11-24_090000.jsonl")
        self.log_file("2026-11-25_090000.jsonl")
        current = self.log_file("2026-11-26_090000.jsonl")
        dump = self.log_file("2026-11-25_090000.compaction-1.txt", b"prompt")
        self.assertEqual(self.memory.upload_leftovers(self.logs, skip=current), 2)
        self.assertEqual(sorted(p.name for p in self.logs.iterdir()),
                         [dump.name, current.name])
        self.assertTrue((self.vault_dir / "logs" / "2026-11-24_090000.jsonl").exists())

    def test_refused_leftover_does_not_block_the_rest(self):
        self.memory.upload_log(self.log_file("2026-11-24_090000.jsonl"))
        refused = self.log_file("2026-11-24_090000.jsonl", b'{"type":"other"}\n')  # 409
        self.log_file("2026-11-25_090000.jsonl")
        self.assertEqual(self.memory.upload_leftovers(self.logs, skip=self.logs / "x"), 1)
        self.assertEqual([p.name for p in self.logs.iterdir()], [refused.name])

    def test_upload_leftovers_with_service_down(self):
        self.log_file("2026-11-24_090000.jsonl")
        self.assertEqual(down_client().upload_leftovers(self.logs, skip=self.logs / "x"), 0)

    def test_system_prompt_block(self):
        self.assertEqual(
            system_prompt("P", {"core": "- a\n", "index": ""}),
            "P\n\n[Long-term memory]\n- a\n\n[Notes you can read]\n(none yet)\n\n" + MEMORY_RULES,
        )
        self.assertEqual(
            system_prompt("P", {"core": "", "index": "x | X | g | 2026-11-26"}),
            "P\n\n[Long-term memory]\n(empty)\n\n[Notes you can read]\nx | X | g | 2026-11-26\n\n"
            + MEMORY_RULES,
        )
        self.assertEqual(system_prompt("P", None), "P")

    def test_from_env_off_without_url(self):
        env = {k: v for k, v in os.environ.items() if k != "MEMORY_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(MemoryClient.from_env(SID))
        with mock.patch.dict(os.environ, {"MEMORY_URL": "http://h:1", "MEMORY_TOKEN": "k"}):
            memory = MemoryClient.from_env(SID)
        self.assertEqual(memory.http.headers["authorization"], "Bearer k")
        self.assertEqual(str(memory.http.base_url).rstrip("/"), "http://h:1")


    def test_bad_config_means_memory_off(self):
        # a typo in .env must not stop Appy from booting
        for env in ({"MEMORY_URL": "http://h:notaport", "MEMORY_TOKEN": "k"},
                    {"MEMORY_URL": "http://h:1", "MEMORY_TOKEN": "t\u00f6k"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertIsNone(MemoryClient.from_env(SID))

    def test_odd_slug_never_raises(self):
        self.assertIn("error", json.loads(down_client().read("a\nb")))
        self.assertIn("error", json.loads(self.memory.read("a\x00b")))

if __name__ == "__main__":
    unittest.main()
