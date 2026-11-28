"""Live check of DJ mode through the real tools (play_music / music_control), wired like
main.py: background player + Riff behind an ActivePlayer. Needs SPOTIFY_CLIENT_ID in .env
and a finished `appy spotify-login`. Plays music for ~30 s, then pauses and stops.

    .venv/bin/python dev/dj_check.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from dj import DJ  # noqa: E402
from player import BackgroundPlayer  # noqa: E402
from riff import ActivePlayer, Riff, RiffError, SpotifydPlayer  # noqa: E402
from spotify import Spotify  # noqa: E402
from spotify_auth import TokenStore  # noqa: E402


def show(label, raw):
    data = json.loads(raw)
    print(f"[{'FAIL' if 'error' in data else 'ok'}]   {label}: {data}")
    return data


def main():
    background = BackgroundPlayer()
    if not background.start():
        print(f"[FAIL] background player: {background.problem}")
        return 1
    riff, appy = Riff(), SpotifydPlayer()
    music = ActivePlayer([appy, riff])

    def pause_riff():
        try:
            if riff.is_playing():
                riff.pause()
        except RiffError:
            pass

    sp = Spotify(TokenStore())
    dj = DJ(music, sp, player_running=background.running, pause_others=pause_riff)
    try:
        print(f"       riff playing at start: {riff.is_playing()}")
        show("play Karma Police", dj.play_music("Karma Police", "track"))
        print(f"       riff playing now: {riff.is_playing()}  active: {type(music.current()).__name__}")
        show("now_playing", dj.music_control("now_playing"))
        show("volume_down", dj.music_control("volume_down"))
        show("volume_up", dj.music_control("volume_up"))
        show("queue Paranoid Android", dj.play_music("Paranoid Android", "track", queue=True))
        show("next", dj.music_control("next"))
        time.sleep(2)
        show("now_playing", dj.music_control("now_playing"))
        mine = sp.my_playlists()
        if mine:
            show(f"play my {mine[0]['name']!r} playlist", dj.play_music(mine[0]["name"], "playlist"))
        show("play some Radiohead", dj.play_music("Radiohead", "artist"))
        show("play my liked songs", dj.play_music("", "liked"))
        show("pause", dj.music_control("pause"))
        time.sleep(1)
        show("resume", dj.music_control("resume"))
        time.sleep(2)
        show("pause", dj.music_control("pause"))
        return 0
    finally:
        background.stop()


if __name__ == "__main__":
    sys.exit(main())
