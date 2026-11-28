"""Live check: can Appy start music in Riff? Needs SPOTIFY_CLIENT_ID in .env, a finished
`appy spotify-login`, and Riff open and logged in. Plays a few seconds of music.

    .venv/bin/python dev/dj_check.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from riff import Riff, RiffError  # noqa: E402
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
    riff, sp = Riff(), Spotify(TokenStore())
    step("riff running", riff.is_running)
    volume = step("riff volume", riff.volume)
    if volume is not None:
        step("riff volume set (0.5, read back)", lambda: (riff.set_volume(0.5), time.sleep(0.3),
                                                          round(riff.volume(), 2))[-1])
        step("riff volume restore", lambda: riff.set_volume(volume))
    devices = step("spotify devices", sp.devices) or []
    match = next((d for d in devices if "riff" in d["name"].lower()), None)
    print(f"       riff device: {match}")
    hit = (step("search 'Karma Police'", lambda: sp.search("Karma Police", "track", 1)) or [None])[0]
    device_id = match["id"] if match else None
    if hit:
        if step("web api play on riff device", lambda: sp.play(device_id, uris=[hit["uri"]])) is None:
            pass
        time.sleep(3)
        step("  -> riff now playing", riff.now_playing)
        user = step("user id", sp.user_id)
        if user:
            step("web api play liked collection",
                 lambda: sp.play(device_id, context_uri=f"spotify:user:{user}:collection"))
            time.sleep(3)
            step("  -> riff now playing", riff.now_playing)
        step("riff OpenUri (track)", lambda: riff.open_uri(hit["uri"]))
        time.sleep(3)
        step("  -> riff now playing", riff.now_playing)
        step("web api queue", lambda: sp.queue(hit["uri"], device_id))
    playlists = step("my playlists (count)", lambda: len(sp.my_playlists()))
    step("riff pause", riff.pause)
    return 0 if playlists is not None else 1


if __name__ == "__main__":
    sys.exit(main())
