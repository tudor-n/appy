"""Live check: can Appy play music on his background player (spotifyd)? Needs
SPOTIFY_CLIENT_ID in .env and a finished `appy spotify-login` (both logins). Starts its
own spotifyd, plays a few seconds of music, then stops it.

    .venv/bin/python dev/dj_check.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from dj import PLAYER_DEVICE_HINT  # noqa: E402
from player import BackgroundPlayer  # noqa: E402
from riff import ActivePlayer, Riff, RiffError, SpotifydPlayer  # noqa: E402
from spotify import Spotify, SpotifyError  # noqa: E402
from spotify_auth import SpotifyAuthError, TokenStore  # noqa: E402


def step(name, fn):
    try:
        result = fn()
        print(f"[ok]   {name}: {result}")
        return result
    except (SpotifyError, SpotifyAuthError, RiffError) as e:
        print(f"[FAIL] {name}: {e}")
    except Exception as e:  # report and keep going: this is a probe
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    return None


def main():
    background = BackgroundPlayer()
    if not background.start():
        print(f"[FAIL] background player: {background.problem}")
        return 1
    try:
        sp, appy, riff = Spotify(TokenStore()), SpotifydPlayer(), Riff()
        active = ActivePlayer([appy, riff])
        device = None
        for _ in range(10):
            time.sleep(1)
            device = next((d for d in sp.devices() if PLAYER_DEVICE_HINT in d["name"].lower()), None)
            if device:
                break
        print(f"[{'ok' if device else 'FAIL'}]   appy device: {device and device['name']}")
        if not device:
            return 1
        hit = (step("search 'Karma Police'", lambda: sp.search("Karma Police", "track", 1)) or [None])[0]
        if hit:
            step("play on appy", lambda: sp.play(device["id"], uris=[hit["uri"]]))
            time.sleep(4)
            step("  -> appy mpris running", appy.is_running)
            step("  -> appy now playing", appy.now_playing)
            step("  -> riff still playing?", riff.is_playing)
            step("  -> active player", lambda: type(active.current()).__name__)
            volume = step("appy volume", appy.volume)
            if volume is not None:
                step("appy volume set 0.3 (read back)",
                     lambda: (appy.set_volume(0.3), time.sleep(0.5), round(appy.volume(), 2))[-1])
                step("appy volume restore", lambda: appy.set_volume(volume))
            step("queue", lambda: sp.queue(hit["uri"], device["id"]))
            step("next (mpris)", appy.next)
            time.sleep(2)
            step("  -> appy now playing", appy.now_playing)
        user = step("user id", sp.user_id)
        if user:
            step("play liked collection",
                 lambda: sp.play(device["id"], context_uri=f"spotify:user:{user}:collection"))
            time.sleep(4)
            step("  -> appy now playing", appy.now_playing)
        step("pause (mpris)", appy.pause)
        return 0
    finally:
        background.stop()


if __name__ == "__main__":
    sys.exit(main())
