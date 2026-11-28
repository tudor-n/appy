"""Spotify Web API as Tudor: search, his playlists and liked songs, and starting playback
on Riff. Transport (pause/skip/volume) doesn't come through here; that's riff.py.

Every error is a SpotifyError (or SpotifyAuthError) whose message is one sentence Appy
can say. Results carry Spotify URIs for dj.py, which keeps them out of what the LLM sees.
"""
import time

import httpx

from spotify_auth import SpotifyAuthError, TokenStore

API = "https://api.spotify.com/v1"
SEARCH_KINDS = ("track", "artist", "album", "playlist")


class SpotifyError(Exception):
    """A Web API call failed; the message is fit to say out loud."""


def _error(response: httpx.Response) -> SpotifyError:
    try:
        err = response.json().get("error") or {}
    except ValueError:
        err = {}
    reason = err.get("reason") if isinstance(err, dict) else None
    status = response.status_code
    if status == 403 and reason == "PREMIUM_REQUIRED":
        return SpotifyError("Spotify needs Premium for that.")
    if status == 403:
        return SpotifyError("Spotify won't let me play that one.")
    if status == 404 and reason == "NO_ACTIVE_DEVICE":
        return SpotifyError("Riff isn't showing up on Spotify.")
    if status == 404:
        return SpotifyError("Spotify couldn't find that.")
    if status == 429:
        return SpotifyError("Spotify's rate-limiting me, try again in a moment.")
    return SpotifyError(f"Spotify had a problem ({status}).")


class Spotify:
    def __init__(self, tokens: TokenStore, http: httpx.Client | None = None,
                 sleep=time.sleep):
        self._tokens = tokens
        self._http = http or httpx.Client(base_url=API, timeout=8.0)
        self._sleep = sleep
        self._user_id: str | None = None

    def _request(self, method: str, path: str, **kwargs) -> dict | None:
        refreshed = retried = False
        while True:
            headers = {"Authorization": f"Bearer {self._tokens.access_token()}"}
            try:
                r = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError:
                raise SpotifyError("I can't reach Spotify right now.") from None
            if r.status_code == 401 and not refreshed:
                refreshed = True
                self._tokens.invalidate()
                continue
            if r.status_code in (500, 502, 503, 504) and not retried:
                retried = True  # Spotify's player API has the odd transient 502
                self._sleep(1.0)
                continue
            if r.status_code == 401:
                self._tokens.forget()  # pick up a new login without a restart
                raise SpotifyAuthError("Spotify logged me out. Run appy spotify-login again.")
            if r.status_code >= 400:
                raise _error(r)
            if r.status_code == 204 or not r.content:
                return None
            try:
                return r.json()
            except ValueError:
                return None

    @staticmethod
    def _device(device_id: str | None) -> dict:
        return {"device_id": device_id} if device_id else {}

    def search(self, query: str, kind: str, limit: int = 5) -> list[dict]:
        if kind not in SEARCH_KINDS:
            raise ValueError(f"unknown search kind: {kind}")
        body = self._request("GET", "/search", params={"q": query, "type": kind, "limit": limit})
        items = ((body or {}).get(f"{kind}s") or {}).get("items") or []
        out = []
        for item in items:
            if not item:  # Spotify sometimes returns nulls in playlist results
                continue
            hit = {"name": item.get("name", ""), "uri": item.get("uri", "")}
            if kind in ("track", "album"):
                artists = item.get("artists") or []
                hit["artist"] = artists[0].get("name", "") if artists else ""
            if kind == "track" and (item.get("album") or {}).get("uri"):
                hit["album_uri"] = item["album"]["uri"]
            elif kind == "playlist":
                hit["owner"] = (item.get("owner") or {}).get("display_name", "")
            out.append(hit)
        return out

    def my_playlists(self) -> list[dict]:
        out: list[dict] = []
        url, params = "/me/playlists", {"limit": 50}
        while url:
            body = self._request("GET", url, params=params) or {}
            out += [{"name": p.get("name", ""), "uri": p.get("uri", "")}
                    for p in body.get("items") or [] if p]
            url, params = body.get("next"), None
        return out

    def liked_tracks(self, limit: int = 100) -> list[str]:
        uris: list[str] = []
        while len(uris) < limit:
            page = min(50, limit - len(uris))
            body = self._request("GET", "/me/tracks",
                                 params={"limit": page, "offset": len(uris)}) or {}
            items = body.get("items") or []
            uris += [i["track"]["uri"] for i in items if i and i.get("track")]
            if not items or not body.get("next"):
                break
        return uris[:limit]

    def music_vocabulary(self, liked: int = 200, artists: int = 30, playlists: int = 10) -> list[str]:
        """Names Whisper should expect: up to `artists` artists of his newest `liked`
        saved tracks (most frequent first), then up to `playlists` playlist names."""
        counts: dict[str, int] = {}
        offset = 0
        while offset < liked:
            body = self._request("GET", "/me/tracks",
                                 params={"limit": min(50, liked - offset), "offset": offset}) or {}
            items = body.get("items") or []
            for item in items:
                for artist in ((item or {}).get("track") or {}).get("artists") or []:
                    if name := artist.get("name"):
                        counts[name] = counts.get(name, 0) + 1
            offset += len(items)
            if not items or not body.get("next"):
                break
        names = sorted(counts, key=lambda n: -counts[n])[:artists]
        lists = [p["name"] for p in self.my_playlists() if p["name"] and p["name"] not in names]
        return names + lists[:playlists]

    def user_id(self) -> str:
        if self._user_id is None:
            self._user_id = (self._request("GET", "/me") or {}).get("id", "")
        return self._user_id

    def devices(self) -> list[dict]:
        body = self._request("GET", "/me/player/devices") or {}
        return [{"id": d.get("id"), "name": d.get("name", ""), "is_active": bool(d.get("is_active"))}
                for d in body.get("devices") or []]

    def play(self, device_id: str | None, context_uri: str | None = None,
             uris: list[str] | None = None, offset_uri: str | None = None) -> None:
        """Play a context (album, playlist, artist; from `offset_uri` if given) or a list
        of tracks. spotifyd can't play bare tracks, so a track goes in its album."""
        if context_uri:
            body = {"context_uri": context_uri}
            if offset_uri:
                body["offset"] = {"uri": offset_uri}
        else:
            body = {"uris": list(uris or [])}
        self._request("PUT", "/me/player/play", params=self._device(device_id), json=body)

    def queue(self, uri: str, device_id: str | None) -> None:
        self._request("POST", "/me/player/queue", params={"uri": uri, **self._device(device_id)})

    def shuffle(self, on: bool, device_id: str | None) -> None:
        self._request("PUT", "/me/player/shuffle",
                      params={"state": "true" if on else "false", **self._device(device_id)})
