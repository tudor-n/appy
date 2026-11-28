"""Riff's MPRIS client, against a fake session bus."""
import unittest

from riff import BUS_NAME, JeepneyBus, Riff, RiffError, RiffNotRunning, _variant


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

    def test_open_uri_passes_the_uri(self):
        bus = FakeBus()
        Riff(bus=bus).open_uri("spotify:track:abc")
        self.assertEqual(bus.calls, [("OpenUri", "spotify:track:abc")])

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

    def test_now_playing_empty_metadata_is_none(self):
        self.assertIsNone(Riff(bus=FakeBus(props={"Metadata": {}})).now_playing())


class RiffLaunchTests(unittest.TestCase):
    def test_ensure_running_launches_and_waits(self):
        polls = {"n": 0}

        def owner():
            polls["n"] += 1
            return polls["n"] >= 3

        launches = []
        clock = FakeClock()
        riff = Riff(bus=FakeBus(owner=owner), launcher=lambda: launches.append(1),
                    clock=clock, sleep=clock.sleep)
        riff.ensure_running(timeout=10.0)
        self.assertEqual(launches, [1])

    def test_ensure_running_times_out(self):
        launches = []
        clock = FakeClock()
        riff = Riff(bus=FakeBus(owner=False), launcher=lambda: launches.append(1),
                    clock=clock, sleep=clock.sleep)
        with self.assertRaises(RiffError):
            riff.ensure_running(timeout=10.0)
        self.assertEqual(launches, [1])
        self.assertGreaterEqual(clock.now, 10.0)

    def test_ensure_running_noop_when_running(self):
        launches = []
        Riff(bus=FakeBus(), launcher=lambda: launches.append(1)).ensure_running()
        self.assertEqual(launches, [])


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
        bus.call("OpenUri", "spotify:track:x", signature="s")
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

    def test_launch_failure_is_a_riff_error(self):
        def no_flatpak():
            raise FileNotFoundError("flatpak")

        riff = Riff(bus=FakeBus(owner=False), launcher=no_flatpak)
        with self.assertRaises(RiffError):
            riff.ensure_running()


if __name__ == "__main__":
    unittest.main()
