"""HEARING-WORKAROUND(respeaker): temporary rules for while the laptop mic hears the music.

Until the ReSpeaker mic array (with its own echo cancellation) arrives, songs playing
through the speakers leak into the mic: lyrics get transcribed as Tudor, and vocals can
hide the pause that ends his turn. With APPY_MIC_HEARS_MUSIC=1, and only while Riff is
playing:
- music commands are forced only for short messages (tools.music_call strict mode), so a
  sung "play it again" doesn't replace the song;
- a turn is closed after UTTERANCE_CAP_S of non-stop "speech" (hearing.over_cap).
Unset, everything behaves exactly as before. Removal: docs/hearing-workaround.md.
"""
import os

UTTERANCE_CAP_S = 15.0


def enabled() -> bool:
    return os.getenv("APPY_MIC_HEARS_MUSIC", "").strip() == "1"


def strict_music(source: str, riff) -> bool:
    """Should this turn's music commands be strict? Only spoken ones, while Riff plays."""
    return enabled() and source == "voice" and riff.is_playing()


def utterance_cap(riff) -> float | None:
    """The longest a spoken turn may run right now, or None for no limit."""
    return UTTERANCE_CAP_S if enabled() and riff.is_playing() else None
