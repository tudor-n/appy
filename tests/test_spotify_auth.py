"""Spotify PKCE login helpers and the refresh-token store."""
import base64
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from spotify_auth import (
    REDIRECT_URI,
    SCOPES,
    SpotifyAuthError,
    TokenStore,
    authorize_url,
    pkce_pair,
)


class Recorder:
    """httpx transport answering the token endpoint with scripted responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body)

    def form(self, i=0) -> dict:
        return {k: v[0] for k, v in parse_qs(self.requests[i].content.decode()).items()}


class PkceTests(unittest.TestCase):
    def test_pkce_challenge_is_s256_of_verifier(self):
        verifier, challenge = pkce_pair()
        self.assertEqual(len(verifier), 64)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        self.assertEqual(challenge, expected.decode().rstrip("="))

    def test_authorize_url_has_scopes_and_redirect(self):
        q = parse_qs(urlparse(authorize_url("cid", "chal", "st8")).query)
        self.assertEqual(q["client_id"], ["cid"])
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["code_challenge"], ["chal"])
        self.assertEqual(q["state"], ["st8"])
        self.assertEqual(q["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(q["scope"], [SCOPES])
        self.assertEqual(REDIRECT_URI, "http://127.0.0.1:8898/callback")
        self.assertEqual(SCOPES, "user-read-playback-state user-modify-playback-state "
                                 "user-read-currently-playing playlist-read-private "
                                 "user-library-read")


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = self.dir / "appy" / "spotify.json"
        self.now = 1_000_000.0

    def store(self, *responses, client_id="cid"):
        self.rec = Recorder(*responses)
        http = httpx.Client(transport=httpx.MockTransport(self.rec))
        return TokenStore(path=self.path, client_id=client_id, http=http, clock=lambda: self.now)

    def write(self, **tokens):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(tokens))

    def test_fresh_token_is_used_without_a_request(self):
        self.write(access_token="A", refresh_token="R", expires_at=self.now + 3600)
        store = self.store()
        self.assertEqual(store.access_token(), "A")
        self.assertEqual(self.rec.requests, [])

    def test_refreshes_when_expired_and_keeps_old_refresh_token(self):
        self.write(access_token="old", refresh_token="R", expires_at=self.now + 30)
        store = self.store((200, {"access_token": "new", "expires_in": 3600}))
        self.assertEqual(store.access_token(), "new")
        form = self.rec.form()
        self.assertEqual(form["grant_type"], "refresh_token")
        self.assertEqual(form["refresh_token"], "R")
        self.assertEqual(form["client_id"], "cid")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["refresh_token"], "R")
        self.assertEqual(saved["expires_at"], self.now + 3600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_saves_rotated_refresh_token(self):
        self.write(access_token="old", refresh_token="R", expires_at=0)
        store = self.store((200, {"access_token": "new", "refresh_token": "R2", "expires_in": 3600}))
        store.access_token()
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "R2")

    def test_invalidate_forces_refresh(self):
        self.write(access_token="A", refresh_token="R", expires_at=self.now + 3600)
        store = self.store((200, {"access_token": "B", "expires_in": 3600}))
        store.invalidate()
        self.assertEqual(store.access_token(), "B")

    def test_revoked_refresh_raises_and_keeps_file(self):
        self.write(access_token="old", refresh_token="R", expires_at=0)
        before = self.path.read_bytes()
        store = self.store((400, {"error": "invalid_grant"}))
        with self.assertRaises(SpotifyAuthError) as ctx:
            store.access_token()
        self.assertIn("spotify-login", str(ctx.exception))
        self.assertEqual(self.path.read_bytes(), before)

    def test_new_login_works_without_restart(self):
        self.write(access_token="old", refresh_token="R", expires_at=0)
        store = self.store((400, {"error": "invalid_grant"}))
        with self.assertRaises(SpotifyAuthError):
            store.access_token()
        # Tudor runs `appy spotify-login`, which writes a fresh token file
        self.write(access_token="fresh", refresh_token="R2", expires_at=self.now + 3600)
        self.assertEqual(store.access_token(), "fresh")

    def test_bad_refresh_response_is_an_auth_error(self):
        self.write(access_token="old", refresh_token="R", expires_at=0)
        store = self.store((200, {"unexpected": True}))
        with self.assertRaises(SpotifyAuthError):
            store.access_token()

    def test_no_client_id_raises(self):
        self.write(access_token="A", refresh_token="R", expires_at=0)
        with self.assertRaises(SpotifyAuthError):
            self.store(client_id="").access_token()

    def test_no_file_raises(self):
        with self.assertRaises(SpotifyAuthError) as ctx:
            self.store().access_token()
        self.assertIn("spotify-login", str(ctx.exception))

    def test_save_creates_private_file(self):
        store = self.store()
        store.save({"access_token": "A", "refresh_token": "R", "expires_in": 3600})
        self.assertEqual(json.loads(self.path.read_text()),
                         {"access_token": "A", "refresh_token": "R", "expires_at": self.now + 3600})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
