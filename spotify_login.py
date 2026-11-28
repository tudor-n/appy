"""One-time Spotify logins for Appy's music tools: `appy spotify-login`.

1. The Web API (search, playlists, starting songs): opens Spotify's consent page, catches
   the redirect on 127.0.0.1:8898 and saves the refresh token to
   ~/.config/appy/spotify.json (readable only by you). Skipped if that login still works.
2. Appy's background player (spotifyd, player.py): `spotifyd authenticate`, another
   browser approval, saved in its cache.
"""
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv

from player import BackgroundPlayer
from spotify_auth import (REDIRECT_URI, TOKEN_URL, SpotifyAuthError, TokenStore,
                          authorize_url, pkce_pair)


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        print("Set SPOTIFY_CLIENT_ID in .env first (see README, 'Music').")
        return 1
    try:
        TokenStore(client_id=client_id).access_token()
        print("Spotify Web API: already logged in.")
    except SpotifyAuthError:
        if web_api_login(client_id) != 0:
            return 1
    player = BackgroundPlayer()
    print("Now Appy's background player (spotifyd): approve it in the browser too.")
    if not player.authenticate():
        print(f"spotifyd isn't installed at {player.binary} (see README, 'Music'), or its login failed.")
        return 1
    print("Logged in. Appy can play music by name now.")
    return 0


def web_api_login(client_id: str) -> int:
    """The PKCE browser login for the Web API; 0 on success."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path != urlparse(REDIRECT_URI).path:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in parse_qs(url.query).items()})
            ok = "code" in result and result.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Appy is logged in to Spotify. You can close this tab."
                              if ok else "Spotify login failed; check the terminal.").encode())

        def log_message(self, *args):  # keep the code out of the terminal
            pass

    url = urlparse(REDIRECT_URI)
    server = HTTPServer((url.hostname, url.port), Callback)
    link = authorize_url(client_id, challenge, state)
    print("Opening Spotify in your browser. If nothing opens, visit:\n" + link)
    webbrowser.open(link)
    while not result:
        server.handle_request()
    server.server_close()

    if result.get("state") != state:
        print("Login failed: the reply didn't match this login attempt.")
        return 1
    if "code" not in result:
        print(f"Login failed: {result.get('error', 'no code returned')}.")
        return 1

    r = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }, timeout=10.0)
    if r.status_code != 200:
        print(f"Login failed: Spotify answered {r.status_code}.")
        return 1
    TokenStore(client_id=client_id).save(r.json())
    print("Spotify Web API: logged in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
