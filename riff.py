"""Music players over MPRIS on the session bus: Riff (dev.diegovsky.Riff, Tudor's GTK
Spotify client) and spotifyd (Appy's own background player), plus ActivePlayer, which
follows whichever of them is playing.

Transport, volume and "what's playing" go through here: local, instant, and no Spotify
login needed. Searching and starting something specific goes through spotify.py.
"""
import threading
import time
from typing import Callable

BUS_NAME = "org.mpris.MediaPlayer2.dev.diegovsky.Riff"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
RIFF_IDLE_TITLE = "Not playing"
# spotifyd takes this name (+ its PID) once it is the playback device
SPOTIFYD_PREFIX = "org.mpris.MediaPlayer2.spotifyd."


class RiffError(Exception):
    """Something went wrong talking to Riff; the message is fit to say out loud."""


class RiffNotRunning(RiffError):
    def __init__(self, message: str = "Riff isn't open."):
        super().__init__(message)


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

    def __init__(self, bus_name: str = BUS_NAME, connect=None):
        self.bus_name = bus_name  # whose MPRIS interface call/get/set talk to
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
        return DBusAddress(OBJECT_PATH, bus_name=self.bus_name, interface=PLAYER)

    def has_owner(self, name: str) -> bool:
        from jeepney.bus_messages import message_bus
        return bool(self._send(message_bus.NameHasOwner(name))[0])

    def list_names(self) -> list[str]:
        from jeepney.bus_messages import message_bus
        return list(self._send(message_bus.ListNames())[0])

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


class MprisPlayer:
    """One MPRIS player. Every call raises RiffNotRunning if it isn't on the bus."""

    BUS_NAME: str | None = None
    NOT_RUNNING = "My music player isn't open."

    def __init__(self, bus=None):
        self._bus = bus if bus is not None else JeepneyBus(self.BUS_NAME or "")

    def _resolve(self) -> str | None:
        return self.BUS_NAME

    def is_running(self) -> bool:
        try:
            name = self._resolve()
            if not name or not self._bus.has_owner(name):
                return False
            self._bus.bus_name = name
            return True
        except Exception:
            return False

    def _require(self):
        if not self.is_running():
            raise RiffNotRunning(self.NOT_RUNNING)

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
        artists = meta.get("xesam:artist") or []
        # Riff's placeholder while nothing is loaded: "Not playing", no artist
        if not title or (title == RIFF_IDLE_TITLE and not artists):
            return None
        return {
            "title": str(title),
            "artist": str(artists[0]) if artists else "",
            "album": str(meta.get("xesam:album") or ""),
        }


class Riff(MprisPlayer):
    """Tudor's Riff. It can't be told to play a specific song (its MPRIS OpenUri isn't
    implemented), so it's only ever controlled, never started or launched, by Appy."""

    BUS_NAME = BUS_NAME
    NOT_RUNNING = "Riff isn't open."


class SpotifydPlayer(MprisPlayer):
    """Appy's background player. Its MPRIS name carries spotifyd's PID and only exists
    while it is the playback device, so it's looked up on every check."""

    NOT_RUNNING = "My music player isn't playing anything."

    def _resolve(self) -> str | None:
        return next((n for n in self._bus.list_names() if n.startswith(SPOTIFYD_PREFIX)), None)


class ActivePlayer:
    """Whichever player Tudor means: the one playing; else the one that played last (so
    "resume" goes back to it); else the first one that's open."""

    def __init__(self, players: list):
        self.players = players
        self._last = None

    def current(self):
        for p in self.players:
            if p.is_playing():
                self._last = p
                return p
        if self._last is not None and self._last.is_running():
            return self._last
        for p in self.players:
            if p.is_running():
                return p
        raise RiffNotRunning("No music player is open.")

    def is_running(self) -> bool:
        return any(p.is_running() for p in self.players)

    def is_playing(self) -> bool:
        return any(p.is_playing() for p in self.players)

    def status(self) -> str:
        return self.current().status()

    def play(self):
        self.current().play()

    def pause(self):
        self.current().pause()

    def next(self):
        self.current().next()

    def previous(self):
        self.current().previous()

    def volume(self) -> float:
        return self.current().volume()

    def set_volume(self, v: float):
        self.current().set_volume(v)

    def shuffle(self, on: bool):
        self.current().shuffle(on)

    def now_playing(self) -> dict | None:
        return self.current().now_playing()
