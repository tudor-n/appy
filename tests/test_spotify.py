"""Spotify Web API client, against httpx.MockTransport."""
import json
import unittest
from urllib.parse import parse_qs

import httpx

from spotify import Spotify, SpotifyError
from spotify_auth import SpotifyAuthError


class FakeTokens:
    def __init__(self, tokens=("T1", "T2", "T3")):
        self.tokens = list(tokens)
        self.invalidated = 0

    def access_token(self):
        return self.tokens[0]

    def invalidate(self):
        self.invalidated += 1
        self.tokens.pop(0)


class Api:
    """Routes (method, path) to a handler returning (status, json-or-None)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        handler = self.routes[(request.method, request.url.path)]
        status, body = handler(request) if callable(handler) else handler
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)


def client(routes, tokens=None):
    api = Api(routes)
    http = httpx.Client(transport=httpx.MockTransport(api), base_url="https://api.spotify.com/v1")
    return Spotify(tokens or FakeTokens(), http=http), api


TRACK = {"name": "Hello", "uri": "spotify:track:h", "artists": [{"name": "Adele"}]}


class SearchTests(unittest.TestCase):
    def test_search_track_returns_name_artist_uri(self):
        sp, api = client({("GET", "/v1/search"): (200, {"tracks": {"items": [TRACK]}})})
        self.assertEqual(sp.search("hello", "track"),
                         [{"name": "Hello", "uri": "spotify:track:h", "artist": "Adele"}])
        q = parse_qs(api.requests[0].url.query.decode())
        self.assertEqual(q["q"], ["hello"])
        self.assertEqual(q["type"], ["track"])
        self.assertEqual(q["limit"], ["5"])
        self.assertEqual(api.requests[0].headers["authorization"], "Bearer T1")

    def test_search_playlist_has_owner_and_skips_nulls(self):
        body = {"playlists": {"items": [None, {"name": "Gym", "uri": "spotify:playlist:g",
                                               "owner": {"display_name": "someone"}}]}}
        sp, _ = client({("GET", "/v1/search"): (200, body)})
        self.assertEqual(sp.search("gym", "playlist"),
                         [{"name": "Gym", "uri": "spotify:playlist:g", "owner": "someone"}])

    def test_search_artist(self):
        body = {"artists": {"items": [{"name": "Radiohead", "uri": "spotify:artist:r"}]}}
        sp, _ = client({("GET", "/v1/search"): (200, body)})
        self.assertEqual(sp.search("radiohead", "artist"),
                         [{"name": "Radiohead", "uri": "spotify:artist:r"}])


class LibraryTests(unittest.TestCase):
    def test_my_playlists_follows_next_pages(self):
        pages = {
            "0": {"items": [{"name": "A", "uri": "spotify:playlist:a"}],
                  "next": "https://api.spotify.com/v1/me/playlists?offset=1&limit=50"},
            "1": {"items": [{"name": "B", "uri": "spotify:playlist:b"}], "next": None},
        }

        def handler(req):
            return 200, pages[parse_qs(req.url.query.decode()).get("offset", ["0"])[0]]

        sp, api = client({("GET", "/v1/me/playlists"): handler})
        self.assertEqual(sp.my_playlists(), [{"name": "A", "uri": "spotify:playlist:a"},
                                             {"name": "B", "uri": "spotify:playlist:b"}])
        self.assertEqual(len(api.requests), 2)

    def test_liked_tracks_pages_to_limit(self):
        def handler(req):
            q = parse_qs(req.url.query.decode())
            offset, limit = int(q["offset"][0]), int(q["limit"][0])
            items = [{"track": {"uri": f"spotify:track:{offset + i}"}} for i in range(limit)]
            return 200, {"items": items, "next": "more"}

        sp, api = client({("GET", "/v1/me/tracks"): handler})
        uris = sp.liked_tracks(100)
        self.assertEqual(len(uris), 100)
        self.assertEqual(uris[0], "spotify:track:0")
        self.assertEqual(uris[-1], "spotify:track:99")
        self.assertEqual(len(api.requests), 2)

    def test_user_id_is_cached(self):
        sp, api = client({("GET", "/v1/me"): (200, {"id": "tudor"})})
        self.assertEqual(sp.user_id(), "tudor")
        self.assertEqual(sp.user_id(), "tudor")
        self.assertEqual(len(api.requests), 1)

    def test_devices(self):
        body = {"devices": [{"id": "d1", "name": "Riff", "is_active": False, "type": "Computer"}]}
        sp, _ = client({("GET", "/v1/me/player/devices"): (200, body)})
        self.assertEqual(sp.devices(), [{"id": "d1", "name": "Riff", "is_active": False}])


class PlaybackTests(unittest.TestCase):
    def test_play_sends_device_and_body(self):
        sp, api = client({("PUT", "/v1/me/player/play"): (204, None)})
        sp.play("abc", context_uri="spotify:playlist:g")
        sp.play("abc", uris=["spotify:track:h"])
        sp.play(None, uris=["spotify:track:h"])
        first, second, third = api.requests
        self.assertEqual(first.url.params["device_id"], "abc")
        self.assertEqual(json.loads(first.content), {"context_uri": "spotify:playlist:g"})
        self.assertEqual(json.loads(second.content), {"uris": ["spotify:track:h"]})
        self.assertNotIn("device_id", third.url.params)

    def test_queue_and_shuffle(self):
        sp, api = client({("POST", "/v1/me/player/queue"): (204, None),
                          ("PUT", "/v1/me/player/shuffle"): (204, None)})
        sp.queue("spotify:track:h", "abc")
        sp.shuffle(True, "abc")
        self.assertEqual(api.requests[0].url.params["uri"], "spotify:track:h")
        self.assertEqual(api.requests[0].url.params["device_id"], "abc")
        self.assertEqual(api.requests[1].url.params["state"], "true")

    def test_401_refreshes_once_then_retries(self):
        seen = []

        def handler(req):
            seen.append(req.headers["authorization"])
            return (401, {"error": {"status": 401}}) if len(seen) == 1 else (204, None)

        tokens = FakeTokens()
        sp, _ = client({("PUT", "/v1/me/player/play"): handler}, tokens)
        sp.play("abc", uris=["spotify:track:h"])
        self.assertEqual(seen, ["Bearer T1", "Bearer T2"])
        self.assertEqual(tokens.invalidated, 1)

    def test_second_401_is_an_auth_error(self):
        sp, _ = client({("GET", "/v1/me"): (401, {"error": {"status": 401}})})
        with self.assertRaises(SpotifyAuthError):
            sp.user_id()


class ErrorMappingTests(unittest.TestCase):
    def check(self, status, body, expected, path=("PUT", "/v1/me/player/play"), **play):
        sp, _ = client({path: (status, body)})
        with self.assertRaises(SpotifyError) as ctx:
            sp.play("abc", **(play or {"uris": ["spotify:track:h"]}))
        self.assertEqual(str(ctx.exception), expected)

    def test_error_mapping(self):
        cases = [
            (403, {"error": {"status": 403, "reason": "PREMIUM_REQUIRED"}}, {},
             "Spotify needs Premium for that."),
            (403, {"error": {"status": 403, "message": "Forbidden"}},
             {"context_uri": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"},
             "Spotify won't let me play that one."),
            (404, {"error": {"status": 404, "reason": "NO_ACTIVE_DEVICE"}}, {},
             "Riff isn't showing up on Spotify."),
            (429, {"error": {"status": 429}}, {},
             "Spotify's rate-limiting me, try again in a moment."),
        ]
        for status, body, play, expected in cases:
            with self.subTest(status=status, expected=expected):
                self.check(status, body, expected, **play)

    def test_network_down(self):
        def boom(request):
            raise httpx.ConnectError("no route")

        http = httpx.Client(transport=httpx.MockTransport(boom), base_url="https://api.spotify.com/v1")
        with self.assertRaises(SpotifyError) as ctx:
            Spotify(FakeTokens(), http=http).devices()
        self.assertEqual(str(ctx.exception), "I can't reach Spotify right now.")

    def test_other_errors_are_generic(self):
        sp, _ = client({("GET", "/v1/me/player/devices"): (500, {"error": {"status": 500}})})
        with self.assertRaises(SpotifyError) as ctx:
            sp.devices()
        self.assertEqual(str(ctx.exception), "Spotify had a problem (500).")


if __name__ == "__main__":
    unittest.main()
