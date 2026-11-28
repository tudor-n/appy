"""Appy's own background player: spotifyd, a headless Spotify Connect device called
"Appy". play_music starts songs on it through the Web API (Riff can't be told to play a
specific song). It runs only while Appy does, so with Appy off nothing changes for Tudor.

Install: the official "full" release binary from github.com/Spotifyd/spotifyd at
~/.local/bin/spotifyd (README, "Music"). Login: `appy spotify-login`.
"""
import subprocess
from pathlib import Path
from typing import Callable

DEFAULT_BINARY = Path.home() / ".local" / "bin" / "spotifyd"
DEFAULT_CACHE = Path.home() / ".cache" / "appy" / "spotifyd"
DEVICE_NAME = "Appy"
STOP_TIMEOUT_S = 3.0
# Where spotifyd's login waits for the browser; its default, 8000, is often taken
OAUTH_PORT = 8897


class BackgroundPlayer:
    def __init__(self, binary: Path = DEFAULT_BINARY, cache: Path = DEFAULT_CACHE,
                 popen: Callable = subprocess.Popen):
        self.binary = Path(binary)
        self.cache = Path(cache)
        self._popen = popen
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
        log = open(self.cache.parent / "spotifyd.log", "ab")
        try:
            self._proc = self._popen(self.command(), stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
        except OSError as e:
            self.problem = f"spotifyd didn't start: {e}"
            return False
        finally:
            log.close()  # the child keeps its own handle
        self.problem = None
        return True

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

    def authenticate(self, run: Callable = subprocess.run) -> bool:
        """spotifyd's one-time browser login (saved in the cache). False if not installed."""
        if not self.binary.is_file():
            return False
        self.cache.mkdir(parents=True, exist_ok=True)
        return run([str(self.binary), "authenticate", "--cache-path", str(self.cache),
                    "--oauth-port", str(OAUTH_PORT)]).returncode == 0
