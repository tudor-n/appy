"""Spotify login (Authorization Code with PKCE) and the stored refresh token.

PKCE needs only the app's client ID (SPOTIFY_CLIENT_ID), no secret. `appy spotify-login`
(spotify_login.py) does the one-time browser login; after that TokenStore keeps an access
token fresh from the refresh token in TOKEN_PATH. Tokens are never printed or logged.
"""
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import httpx

TOKEN_PATH = Path.home() / ".config" / "appy" / "spotify.json"
REDIRECT_URI = "http://127.0.0.1:8898/callback"
SCOPES = ("user-read-playback-state user-modify-playback-state "
          "user-read-currently-playing playlist-read-private user-library-read")
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
# Refresh this long before the access token actually expires
EXPIRY_MARGIN_S = 60

NOT_LOGGED_IN = "Spotify isn't logged in. Run appy spotify-login."


class SpotifyAuthError(Exception):
    """No usable Spotify login; the message is what Appy tells Tudor."""


def pkce_pair() -> tuple[str, str]:
    """A PKCE (verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def authorize_url(client_id: str, challenge: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "scope": SCOPES,
    })


class TokenStore:
    def __init__(self, path: Path = TOKEN_PATH, client_id: str | None = None,
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.client_id = client_id if client_id is not None else os.getenv("SPOTIFY_CLIENT_ID", "")
        self._http = http or httpx.Client(timeout=10.0)
        self._clock = clock
        self._tokens: dict | None = None

    def _load(self) -> dict:
        if self._tokens is None:
            try:
                self._tokens = json.loads(self.path.read_text())
            except (OSError, ValueError):
                raise SpotifyAuthError(NOT_LOGGED_IN) from None
        return self._tokens

    def access_token(self) -> str:
        if not self.client_id:
            raise SpotifyAuthError(NOT_LOGGED_IN)
        tokens = self._load()
        if tokens.get("access_token") and tokens.get("expires_at", 0) - EXPIRY_MARGIN_S > self._clock():
            return tokens["access_token"]
        if not tokens.get("refresh_token"):
            raise SpotifyAuthError(NOT_LOGGED_IN)
        try:
            r = self._http.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": self.client_id,
            })
        except httpx.TransportError:
            raise SpotifyAuthError("I can't reach Spotify right now.") from None
        if r.status_code == 400:
            self.forget()  # a new `appy spotify-login` is picked up without a restart
            raise SpotifyAuthError("Spotify logged me out. Run appy spotify-login again.")
        try:
            if r.status_code != 200:
                raise ValueError(r.status_code)
            self.save(r.json())
        except (ValueError, KeyError, TypeError):
            raise SpotifyAuthError("Spotify wouldn't refresh my login right now.") from None
        return self._tokens["access_token"]

    def forget(self) -> None:
        """Drop the cached tokens; the next access_token() re-reads the file."""
        self._tokens = None

    def invalidate(self) -> None:
        """Make the next access_token() refresh (after a 401)."""
        try:
            self._load()["expires_at"] = 0
        except SpotifyAuthError:
            pass  # nothing stored; access_token() will say so

    def save(self, response: dict) -> None:
        """Store a token response; keep the old refresh token if it brought none."""
        old = self._tokens or {}
        self._tokens = {
            "access_token": response["access_token"],
            "refresh_token": response.get("refresh_token") or old.get("refresh_token"),
            "expires_at": self._clock() + float(response.get("expires_in", 3600)),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".spotify-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._tokens, f)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
