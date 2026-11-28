"""Riff's MPRIS client, against a fake session bus."""
import unittest

from riff import (BUS_NAME, ActivePlayer, JeepneyBus, Riff, RiffError, RiffNotRunning,
                  SpotifydPlayer, _variant)


class FakeBus:
    def __init__(self, owner=True, props=None):
        self.owner = owner
        self.props = dict(props or {})
        self.calls: list[tuple] = []
        self.sets: list[tuple] = []

    def has_owner(self, name):
        assert name == BUS_NAME
        return self.owner() if callable(self.owner) else self.owner

    def call(self, method, *args, signature=""):
        self.calls.append((method, *args))

    def get(self, prop):
        return self.props[prop]

    def set(self, prop, signature, value):
        self.sets.append((prop, signature, value))
        self.props[prop] = value


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class RiffTransportTests(unittest.TestCase):
    def test_transport_calls_mpris_methods(self):
        bus = FakeBus()
        riff = Riff(bus=bus)
        riff.play()
        riff.pause()
        riff.next()
        riff.previous()
        self.assertEqual([c[0] for c in bus.calls], ["Play", "Pause", "Next", "Previous"])

    def test_not_running_raises(self):
        riff = Riff(bus=FakeBus(owner=False))
        with self.assertRaises(RiffNotRunning):
            riff.pause()
        with self.assertRaises(RiffNotRunning):
            riff.volume()
        self.assertFalse(riff.is_playing())
        self.assertFalse(riff.is_running())

    def test_not_running_is_a_riff_error(self):
        self.assertTrue(issubclass(RiffNotRunning, RiffError))

    def test_status_and_is_playing(self):
        bus = FakeBus(props={"PlaybackStatus": "Playing"})
        riff = Riff(bus=bus)
        self.assertEqual(riff.status(), "Playing")
        self.assertTrue(riff.is_playing())
        bus.props["PlaybackStatus"] = "Paused"
        self.assertFalse(riff.is_playing())

    def test_is_playing_never_raises(self):
        class Broken(FakeBus):
            def get(self, prop):
                raise OSError("bus gone")
        self.assertFalse(Riff(bus=Broken()).is_playing())

    def test_set_volume_clamps(self):
        bus = FakeBus(props={"Volume": 0.5})
        riff = Riff(bus=bus)
        riff.set_volume(1.7)
        self.assertEqual(bus.props["Volume"], 1.0)
        riff.set_volume(-1)
        self.assertEqual(bus.props["Volume"], 0.0)
        self.assertEqual(bus.sets[0][1], "d")

    def test_volume_reads_property(self):
        self.assertEqual(Riff(bus=FakeBus(props={"Volume": 0.42})).volume(), 0.42)

    def test_shuffle(self):
        bus = FakeBus()
        Riff(bus=bus).shuffle(True)
        self.assertEqual(bus.sets, [("Shuffle", "b", True)])

    def test_now_playing_reads_metadata(self):
        bus = FakeBus(props={"Metadata": {
            "xesam:title": "Karma Police",
            "xesam:artist": ["Radiohead"],
            "xesam:album": "OK Computer",
        }})
        self.assertEqual(Riff(bus=bus).now_playing(),
                         {"title": "Karma Police", "artist": "Radiohead", "album": "OK Computer"})

    def test_now_playing_placeholder_is_none(self):
        # Riff fills Metadata with a placeholder while nothing is loaded (seen live)
        bus = FakeBus(props={"Metadata": {"xesam:title": "Not playing", "xesam:artist": [],
                                          "xesam:album": ""}})
        self.assertIsNone(Riff(bus=bus).now_playing())

    def test_now_playing_empty_metadata_is_none(self):
        self.assertIsNone(Riff(bus=FakeBus(props={"Metadata": {}})).now_playing())


class VariantTests(unittest.TestCase):
    def test_unwraps_metadata_variants(self):
        raw = ("a{sv}", {"xesam:title": ("s", "X"), "xesam:artist": ("as", ["A", "B"])})
        self.assertEqual(_variant(raw), {"xesam:title": "X", "xesam:artist": ["A", "B"]})

    def test_unwraps_plain_variant(self):
        self.assertEqual(_variant(("d", 0.5)), 0.5)


class FakeConnection:
    """jeepney blocking connection stand-in: answers or raises as scripted."""

    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises
        self.sent = []
        self.closed = False

    def send_and_get_reply(self, msg, timeout=None):
        msg.serialise(serial=1)  # what the real connection does first
        self.sent.append(msg)
        if self.raises:
            raise self.raises
        return self.reply(msg) if callable(self.reply) else self.reply

    def close(self):
        self.closed = True


def ok_reply(msg):
    from jeepney import new_method_return
    return new_method_return(msg)


class JeepneyBusTests(unittest.TestCase):
    def test_method_calls_without_arguments_serialise(self):
        conn = FakeConnection(reply=ok_reply)
        bus = JeepneyBus(connect=lambda: conn)
        for method in ("Play", "Pause", "Next", "Previous"):
            bus.call(method)
        bus.call("Seek", 5_000_000, signature="x")
        self.assertEqual(len(conn.sent), 5)

    def test_dbus_error_reply_becomes_riff_error(self):
        from jeepney import new_error

        conn = FakeConnection(reply=lambda m: new_error(m, "org.freedesktop.DBus.Error.ServiceUnknown"))
        with self.assertRaises(RiffError):
            JeepneyBus(connect=lambda: conn).call("Play")

    def test_timeout_becomes_riff_error_and_reconnects(self):
        conns = [FakeConnection(raises=TimeoutError()), FakeConnection(reply=ok_reply)]
        made = list(conns)
        bus = JeepneyBus(connect=lambda: made.pop(0))
        with self.assertRaises(RiffError):
            bus.call("Play")
        self.assertTrue(conns[0].closed)
        bus.call("Play")  # a fresh connection
        self.assertEqual(len(conns[1].sent), 1)


class SpotifydBus(FakeBus):
    """A bus where spotifyd's MPRIS name carries its PID."""

    def __init__(self, names, **kw):
        super().__init__(**kw)
        self.names = names
        self.bus_name = None

    def list_names(self):
        return list(self.names)

    def has_owner(self, name):
        return name in self.names


class SpotifydPlayerTests(unittest.TestCase):
    def test_finds_its_instance_name(self):
        bus = SpotifydBus(["org.freedesktop.DBus", "org.mpris.MediaPlayer2.spotifyd.instance4242"],
                          props={"PlaybackStatus": "Playing"})
        player = SpotifydPlayer(bus=bus)
        self.assertTrue(player.is_running())
        self.assertEqual(bus.bus_name, "org.mpris.MediaPlayer2.spotifyd.instance4242")
        self.assertTrue(player.is_playing())

    def test_uses_only_its_own_instance(self):
        # A leftover or hand-started spotifyd must not be mistaken for Appy's
        bus = SpotifydBus(["org.mpris.MediaPlayer2.spotifyd.instance111",
                           "org.mpris.MediaPlayer2.spotifyd.instance4242"])
        player = SpotifydPlayer(bus=bus, pid=lambda: 4242)
        self.assertTrue(player.is_running())
        self.assertEqual(bus.bus_name, "org.mpris.MediaPlayer2.spotifyd.instance4242")
        self.assertFalse(SpotifydPlayer(bus=bus, pid=lambda: None).is_running())

    def test_not_running_without_the_name(self):
        player = SpotifydPlayer(bus=SpotifydBus(["rs.spotifyd.instance4242"]))
        self.assertFalse(player.is_running())
        with self.assertRaises(RiffNotRunning) as ctx:
            player.pause()
        self.assertEqual(str(ctx.exception), "My music player isn't playing anything.")


class StubPlayer:
    def __init__(self, name, running=True, status="Paused"):
        self.name, self.running, self._status = name, running, status
        self.calls = []

    def is_running(self):
        return self.running

    def is_playing(self):
        return self.running and self._status == "Playing"

    def status(self):
        if not self.running:
            raise RiffNotRunning()
        return self._status

    def pause(self):
        self.calls.append("pause")

    def volume(self):
        return 0.5


class ActivePlayerTests(unittest.TestCase):
    def test_prefers_the_one_playing(self):
        appy, riff = StubPlayer("appy"), StubPlayer("riff", status="Playing")
        active = ActivePlayer([appy, riff])
        self.assertIs(active.current(), riff)
        active.pause()
        self.assertEqual(riff.calls, ["pause"])
        self.assertTrue(active.is_playing())

    def test_remembers_the_last_one_that_played(self):
        appy, riff = StubPlayer("appy"), StubPlayer("riff", status="Playing")
        active = ActivePlayer([appy, riff])
        active.current()
        riff._status = "Paused"  # Tudor paused Riff; "resume" should go back to Riff
        self.assertIs(active.current(), riff)
        self.assertFalse(active.is_playing())

    def test_falls_back_to_first_running(self):
        appy, riff = StubPlayer("appy", running=False), StubPlayer("riff")
        self.assertIs(ActivePlayer([appy, riff]).current(), riff)

    def test_nothing_running(self):
        active = ActivePlayer([StubPlayer("appy", running=False), StubPlayer("riff", running=False)])
        self.assertFalse(active.is_running())
        self.assertFalse(active.is_playing())
        with self.assertRaises(RiffNotRunning) as ctx:
            active.pause()
        self.assertEqual(str(ctx.exception), "No music player is open.")

    def test_delegates_reads(self):
        self.assertEqual(ActivePlayer([StubPlayer("riff")]).volume(), 0.5)
        self.assertEqual(ActivePlayer([StubPlayer("riff")]).status(), "Paused")


if __name__ == "__main__":
    unittest.main()
