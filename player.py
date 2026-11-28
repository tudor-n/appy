"""Appy's own background player: spotifyd, a headless Spotify Connect device called
"Appy". play_music starts songs on it through the Web API (Riff can't be told to play a
specific song). It runs only while Appy does, so with Appy off nothing changes for Tudor.

Install: the official "full" release binary from github.com/Spotifyd/spotifyd at
~/.local/bin/spotifyd (README, "Music"). Login: `appy spotify-login`.
"""
import ctypes
import os
import signal
import subprocess
from pathlib import Path
from typing import Callable

DEFAULT_BINARY = Path.home() / ".local" / "bin" / "spotifyd"
DEFAULT_CACHE = Path.home() / ".cache" / "appy" / "spotifyd"
DEVICE_NAME = "Appy"
STOP_TIMEOUT_S = 3.0
# Where spotifyd's login waits for the browser; its default, 8000, is often taken
OAUTH_PORT = 8897


PR_SET_PDEATHSIG = 1


def _die_with_parent():
    """In the child, before exec: get SIGTERM when Appy dies (crash, kill, Ctrl+C during
    boot), so no spotifyd outlives him."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except OSError:
        pass


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


class BackgroundPlayer:
    def __init__(self, binary: Path = DEFAULT_BINARY, cache: Path = DEFAULT_CACHE,
                 popen: Callable = subprocess.Popen, cmdline: Callable[[int], str] = _cmdline,
                 kill: Callable[[int, int], None] = os.kill):
        self.binary = Path(binary)
        self.cache = Path(cache)
        self._popen = popen
        self._cmdline = cmdline
        self._kill = kill
        self._proc = None
        self.problem: str | None = None  # why it isn't running, for the console

    def command(self) -> list[str]:
        return [
            str(self.binary), "--no-daemon", "--use-mpris",
            "--device-name", DEVICE_NAME, "--device-type", "computer",
            "--backend", "pulseaudio", "--bitrate", "320", "--volume-normalisation",
            "--disable-discovery", "--cache-path", str(self.cache),
        ]

    def _credentials(self) -> Path:
        return self.cache / "oauth" / "credentials.json"

    def start(self) -> bool:
        """Start spotifyd; False (and `problem` set) if it isn't installed or logged in."""
        if not self.binary.is_file():
            self.problem = f"spotifyd not installed ({self.binary}); see README, 'Music'"
            return False
        if not self._credentials().is_file():
            self.problem = "spotifyd isn't logged in; run appy spotify-login"
            return False
        self.cache.mkdir(parents=True, exist_ok=True)
        self._stop_leftover()
        log = open(self.cache.parent / "spotifyd.log", "ab")
        try:
            self._proc = self._popen(self.command(), stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, preexec_fn=_die_with_parent)
        except OSError as e:
            self.problem = f"spotifyd didn't start: {e}"
            return False
        finally:
            log.close()  # the child keeps its own handle
        self._pidfile().write_text(str(self._proc.pid))
        self.problem = None
        return True

    def _pidfile(self) -> Path:
        return self.cache / "spotifyd.pid"

    def _stop_leftover(self) -> None:
        """Stop a spotifyd an earlier Appy left running (only if it really is ours: our
        binary with our cache), so there's only ever one "Appy" device."""
        try:
            pid = int(self._pidfile().read_text().strip())
        except (OSError, ValueError):
            return
        line = self._cmdline(pid)
        if "spotifyd" in line and str(self.cache) in line:
            try:
                self._kill(pid, signal.SIGTERM)
            except OSError:
                pass
        self._pidfile().unlink(missing_ok=True)

    def pid(self) -> int | None:
        """spotifyd's PID while it runs (its MPRIS name carries it)."""
        return self._proc.pid if self.running() else None

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._pidfile().unlink(missing_ok=True)

    def authenticate(self, run: Callable = subprocess.run) -> bool:
        """spotifyd's one-time browser login (saved in the cache). False if not installed."""
        if not self.binary.is_file():
            return False
        self.cache.mkdir(parents=True, exist_ok=True)
        return run([str(self.binary), "authenticate", "--cache-path", str(self.cache),
                    "--oauth-port", str(OAUTH_PORT)]).returncode == 0
