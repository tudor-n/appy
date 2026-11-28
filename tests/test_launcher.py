"""bin/appy keeps the checkout on origin/main: tested against throwaway git repos."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "appy"


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class LauncherUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origin = self.tmp / "origin.git"
        self.seed = self.tmp / "seed"   # where "upstream" commits are made
        self.app = self.tmp / "app"     # stands in for ~/dev/appy-main
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.tmp, "clone", "-q", str(self.origin), str(self.seed))
        for name, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            git(self.seed, "config", name, value)
        self.write(self.seed, "bin/appy", LAUNCHER.read_text(), executable=True)
        self.write(self.seed, "appy-ui/src/main.rs", "fn main() {}\n")
        self.write(self.seed, "appy-pill/contents/ui/main.qml", "Item {}\n")
        self.write(self.seed, "requirements.txt", "numpy\n")
        self.write(self.seed, "main.py", "print('v1')\n")
        self.commit_upstream("initial")
        git(self.tmp, "clone", "-q", str(self.origin), str(self.app))
        for name, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            git(self.app, "config", name, value)

        # Stand-ins for cargo and kpackagetool6 that record how they were called
        self.calls = self.tmp / "calls.log"
        stub = self.tmp / "stub"
        stub.write_text(f'#!/bin/sh\necho "$(basename "$0") $*" >> "{self.calls}"\n')
        stub.chmod(0o755)
        for tool in ("cargo", "kpackagetool6"):
            (self.tmp / tool).symlink_to(stub)

    def write(self, repo, path, text, executable=False):
        f = Path(repo) / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        if executable:
            f.chmod(0o755)

    def commit_upstream(self, message, files=None):
        for path, text in (files or {}).items():
            self.write(self.seed, path, text, executable=path.startswith("bin/"))
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", message)
        git(self.seed, "push", "-q", "origin", "HEAD:main")

    def run_launcher(self, *args):
        env = dict(os.environ, APPY_CARGO=str(self.tmp / "cargo"),
                   APPY_KPACKAGETOOL=str(self.tmp / "kpackagetool6"))
        env.pop("APPY_UPDATED_FROM", None)
        proc = subprocess.run([str(self.app / "bin" / "appy"), *args], env=env,
                              capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout + proc.stderr

    def tool_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def head(self):
        return git(self.app, "rev-parse", "HEAD")

    def origin_head(self):
        return git(self.seed, "rev-parse", "HEAD")

    def test_fast_forwards_to_origin_main(self):
        self.commit_upstream("new brain", {"main.py": "print('v2')\n"})
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), self.origin_head())
        self.assertIn("updated", out)
        self.assertEqual((self.app / "main.py").read_text(), "print('v2')\n")
        self.assertEqual(self.tool_calls(), [])  # nothing to build or reinstall

    def test_up_to_date_is_a_no_op(self):
        before = self.head()
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), before)
        self.assertIn("up to date", out)

    def test_local_changes_block_the_update(self):
        self.commit_upstream("new brain", {"main.py": "print('v2')\n"})
        self.write(self.app, "main.py", "print('my experiment')\n")
        before = self.head()
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), before)
        self.assertEqual((self.app / "main.py").read_text(), "print('my experiment')\n")
        self.assertIn("not updating", out)

    def test_untracked_files_do_not_block_the_update(self):
        self.commit_upstream("new brain", {"main.py": "print('v2')\n"})
        self.write(self.app, "scratch.txt", "notes\n")
        self.run_launcher("--update")
        self.assertEqual(self.head(), self.origin_head())

    def test_other_branches_are_not_updated(self):
        git(self.app, "switch", "-q", "-c", "experiment")
        self.commit_upstream("new brain", {"main.py": "print('v2')\n"})
        before = self.head()
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), before)
        self.assertIn("experiment", out)

    def test_diverged_main_is_left_alone(self):
        self.write(self.app, "local.txt", "x\n")
        git(self.app, "add", "-A")
        git(self.app, "commit", "-q", "-m", "local work")
        self.commit_upstream("new brain", {"main.py": "print('v2')\n"})
        before = self.head()
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), before)
        self.assertIn("diverged", out)

    def test_unreachable_origin_still_succeeds(self):
        git(self.app, "remote", "set-url", "origin", str(self.tmp / "nowhere.git"))
        before = self.head()
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), before)
        self.assertIn("not updating", out)

    def test_overlay_changes_are_rebuilt(self):
        self.commit_upstream("ui change", {"appy-ui/src/main.rs": "fn main() { }\n"})
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.tool_calls()), 1, self.tool_calls())
        self.assertTrue(self.tool_calls()[0].startswith("cargo build --release"), self.tool_calls())

    def test_pill_changes_are_reinstalled(self):
        self.commit_upstream("pill", {"appy-pill/contents/ui/main.qml": "Item { }\n"})
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tool_calls(),
                         [f"kpackagetool6 -t Plasma/Applet -u {self.app}/appy-pill"])
        self.assertIn("plasmashell", out)

    def test_requirements_changes_are_flagged_not_installed(self):
        self.commit_upstream("deps", {"requirements.txt": "numpy\nrequests\n"})
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertIn("requirements.txt changed", out)
        self.assertIn("onnxruntime-gpu", out)

    def test_a_new_launcher_takes_over_after_updating(self):
        new = LAUNCHER.read_text().replace(
            "set -euo pipefail\n", 'set -euo pipefail\necho "appy: launcher v2 running"\n', 1
        )
        self.commit_upstream("launcher", {"bin/appy": new, "appy-ui/src/main.rs": "fn main() { }\n"})
        code, out = self.run_launcher("--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), self.origin_head())
        self.assertIn("launcher v2 running", out)
        self.assertEqual(out.count("updated"), 1, out)
        # The follow-ups still see everything the update brought in
        self.assertTrue(any(c.startswith("cargo build") for c in self.tool_calls()))


    def test_spotify_login_runs_the_login_script(self):
        # A stand-in venv python that records what it was asked to run
        py = self.app / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text(f'#!/bin/sh\necho "python $*" >> "{self.calls}"\n')
        py.chmod(0o755)
        code, out = self.run_launcher("spotify-login")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tool_calls(), [f"python {self.app / 'spotify_login.py'}"])
        self.assertEqual(self.head(), self.origin_head())  # no update, no build


if __name__ == "__main__":
    unittest.main()
