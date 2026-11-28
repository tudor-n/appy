"""Appy's background player (spotifyd): command line, start/stop, login."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from player import BackgroundPlayer


class FakeProc:
    def __init__(self, exits_on_terminate=True):
        self.exits_on_terminate = exits_on_terminate
        self.terminated = self.killed = False

    def poll(self):
        return 0 if (self.killed or (self.terminated and self.exits_on_terminate)) else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        if self.poll() is None:
            raise subprocess.TimeoutExpired("spotifyd", timeout)
        return 0

    def kill(self):
        self.killed = True


class BackgroundPlayerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.binary = self.dir / "spotifyd"
        self.binary.write_text("#!/bin/sh\n")
        self.binary.chmod(0o755)
        self.cache = self.dir / "cache"
        self.launched = []
        self.proc = FakeProc()

    def popen(self, cmd, **kw):
        self.launched.append((cmd, kw))
        return self.proc

    def player(self, binary=None):
        return BackgroundPlayer(binary=binary or self.binary, cache=self.cache, popen=self.popen)

    def login(self):
        (self.cache / "oauth").mkdir(parents=True)
        (self.cache / "oauth" / "credentials.json").write_text("{}")

    def test_command_line(self):
        self.assertEqual(self.player().command(), [
            str(self.binary), "--no-daemon", "--use-mpris", "--device-name", "Appy",
            "--device-type", "computer", "--backend", "pulseaudio", "--bitrate", "320",
            "--volume-normalisation", "--disable-discovery", "--cache-path", str(self.cache),
        ])

    def test_starts_when_installed_and_logged_in(self):
        self.login()
        player = self.player()
        self.assertTrue(player.start())
        self.assertEqual(self.launched[0][0], player.command())
        self.assertTrue(player.running())

    def test_missing_binary_does_not_start(self):
        self.login()
        player = self.player(binary=self.dir / "nope")
        self.assertFalse(player.start())
        self.assertEqual(self.launched, [])
        self.assertIn("not installed", player.problem)

    def test_missing_login_does_not_start(self):
        player = self.player()
        self.assertFalse(player.start())
        self.assertEqual(self.launched, [])
        self.assertIn("spotify-login", player.problem)

    def test_stop_terminates(self):
        self.login()
        player = self.player()
        player.start()
        player.stop()
        self.assertTrue(self.proc.terminated)
        self.assertFalse(self.proc.killed)
        self.assertFalse(player.running())

    def test_stop_kills_a_stuck_player(self):
        self.login()
        self.proc = FakeProc(exits_on_terminate=False)
        player = self.player()
        player.start()
        player.stop()
        self.assertTrue(self.proc.killed)

    def test_stop_without_start_is_harmless(self):
        self.player().stop()

    def test_authenticate_runs_spotifyd_auth(self):
        runs = []
        ok = self.player().authenticate(run=lambda cmd, **kw: runs.append(cmd) or
                                        subprocess.CompletedProcess(cmd, 0))
        self.assertTrue(ok)
        # its own port: spotifyd's default 8000 is often taken (e.g. by a dev server)
        self.assertEqual(runs, [[str(self.binary), "authenticate", "--cache-path", str(self.cache),
                                 "--oauth-port", "8897"]])

    def test_authenticate_without_binary(self):
        self.assertFalse(self.player(binary=self.dir / "nope").authenticate(
            run=lambda *a, **k: self.fail("must not run")))


if __name__ == "__main__":
    unittest.main()
