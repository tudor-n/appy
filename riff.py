"""Riff (dev.diegovsky.Riff, a GTK Spotify client) over MPRIS on the session bus.

Transport, volume and "what's playing" go through here: local, instant, and no Spotify
login needed. Searching and starting something specific goes through spotify.py.
"""
import subprocess
import threading
import time
from typing import Callable

BUS_NAME = "org.mpris.MediaPlayer2.dev.diegovsky.Riff"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
FLATPAK_ID = "dev.diegovsky.Riff"


class RiffError(Exception):
    """Something went wrong talking to Riff; the message is fit to say out loud."""


class RiffNotRunning(RiffError):
    def __init__(self):
        super().__init__("Riff isn't open.")


def _launch_riff():
    subprocess.Popen(
        ["flatpak", "run", FLATPAK_ID],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _variant(value):
    """jeepney hands a variant over as (signature, value); unwrap it, and the variants
    nested in an a{sv} (MPRIS Metadata) or av."""
    sig, inner = value
    if sig == "a{sv}":
        return {k: _variant(v) for k, v in inner.items()}
    if sig == "av":
        return [_variant(v) for v in inner]
    if sig == "v":
        return _variant(inner)
    return inner


class JeepneyBus:
    """The only code that touches D-Bus. One blocking connection, opened lazily. Every
    failure (an error reply, a timeout, a dropped bus) comes out as a RiffError, because
    callers include the mic and speaker threads, which must never die of one."""

    TIMEOUT_S = 0.5  # Riff answers in milliseconds; a stall must not hold up audio threads

    def __init__(self, connect=None):
        self._connect = connect or self._open
        self._conn = None
        self._lock = threading.Lock()

    @staticmethod
    def _open():
        from jeepney.io.blocking import open_dbus_connection
        return open_dbus_connection(bus="SESSION")

    def _send(self, msg):
        from jeepney.wrappers import DBusErrorResponse, unwrap_msg
        with self._lock:
            try:
                if self._conn is None:
                    self._conn = self._connect()
                reply = self._conn.send_and_get_reply(msg, timeout=self.TIMEOUT_S)
                return unwrap_msg(reply)
            except DBusErrorResponse as e:
                raise RiffError("Riff didn't answer.") from e
            except (TimeoutError, OSError, ConnectionError, ValueError) as e:
                self._drop()  # reconnect next time
                raise RiffError("Riff didn't answer.") from e

    def _drop(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def _address(self):
        from jeepney import DBusAddress
        return DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=PLAYER)

    def has_owner(self, name: str) -> bool:
        from jeepney.bus_messages import message_bus
        return bool(self._send(message_bus.NameHasOwner(name))[0])

    def call(self, method: str, *args, signature: str = ""):
        from jeepney import new_method_call
        # jeepney needs a tuple body, also an empty one
        return self._send(new_method_call(self._address(), method, signature, tuple(args)))

    def get(self, prop: str):
        from jeepney import Properties
        return _variant(self._send(Properties(self._address()).get(prop))[0])

    def set(self, prop: str, signature: str, value):
        from jeepney import Properties
        self._send(Properties(self._address()).set(prop, signature, value))


class Riff:
    def __init__(self, bus=None, launcher: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._bus = bus if bus is not None else JeepneyBus()
        self._launch = launcher or _launch_riff
        self._clock = clock
        self._sleep = sleep

    def is_running(self) -> bool:
        try:
            return self._bus.has_owner(BUS_NAME)
        except Exception:
            return False

    def ensure_running(self, timeout: float = 10.0) -> None:
        """Start Riff if it isn't open, and wait for it to show up on the bus."""
        if self.is_running():
            return
        try:
            self._launch()
        except OSError as e:  # e.g. flatpak missing
            raise RiffError("Riff didn't start.") from e
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            self._sleep(0.25)
            if self.is_running():
                return
        raise RiffError("Riff didn't start.")

    def _require(self):
        if not self.is_running():
            raise RiffNotRunning()

    def _call(self, method: str, *args, signature: str = ""):
        self._require()
        self._bus.call(method, *args, signature=signature)

    def _get(self, prop: str):
        self._require()
        return self._bus.get(prop)

    def status(self) -> str:
        return str(self._get("PlaybackStatus"))

    def is_playing(self) -> bool:
        try:
            return self.status() == "Playing"
        except Exception:
            return False

    def play(self):
        self._call("Play")

    def pause(self):
        self._call("Pause")

    def next(self):
        self._call("Next")

    def previous(self):
        self._call("Previous")

    def open_uri(self, uri: str):
        self._call("OpenUri", uri, signature="s")

    def volume(self) -> float:
        return float(self._get("Volume"))

    def set_volume(self, v: float):
        self._require()
        self._bus.set("Volume", "d", min(1.0, max(0.0, float(v))))

    def shuffle(self, on: bool):
        self._require()
        self._bus.set("Shuffle", "b", bool(on))

    def now_playing(self) -> dict | None:
        meta = self._get("Metadata") or {}
        title = meta.get("xesam:title")
        if not title:
            return None
        artists = meta.get("xesam:artist") or []
        return {
            "title": str(title),
            "artist": str(artists[0]) if artists else "",
            "album": str(meta.get("xesam:album") or ""),
        }
