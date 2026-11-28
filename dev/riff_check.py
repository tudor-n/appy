"""Live check of Riff's MPRIS interface. Open Riff and start any song, then run:

    .venv/bin/python dev/riff_check.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from riff import Riff, RiffError  # noqa: E402


def main():
    riff = Riff()
    print(f"running:     {riff.is_running()}")
    if not riff.is_running():
        print("Open Riff first.")
        return 1
    try:
        print(f"status:      {riff.status()}")
        print(f"now playing: {riff.now_playing()}")
        before = riff.volume()
        print(f"volume:      {before:.2f}")
        riff.set_volume(0.5)
        time.sleep(0.3)
        after = riff.volume()
        riff.set_volume(before)
        print(f"set 0.50 ->  read back {after:.2f} ({'works' if abs(after - 0.5) < 0.02 else 'IGNORED'})")
    except RiffError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
