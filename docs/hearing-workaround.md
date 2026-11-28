# Hearing workaround (temporary, until the ReSpeaker)

**Tag:** `HEARING-WORKAROUND(respeaker)`. Find every piece with:

```sh
grep -rn "HEARING-WORKAROUND(respeaker)" --exclude-dir=target --exclude-dir=.venv .
```

## Why it exists

Appy's mic is always open, and right now it's the laptop's built-in mic sitting next to
the laptop speakers. When Riff plays music, the song leaks into the mic:

- lyrics get transcribed as if Tudor said them, so a sung "play it again" or "skip"
  could control the music;
- song vocals look like speech to the voice detector, which can keep a turn open forever,
  because the pause that ends Tudor's sentence never shows up.

The planned fix is hardware: a ReSpeaker mic array, which does echo cancellation on the
board, with Pebble V3 speakers. Everything below is a stopgap until then.

**Hardware note:** the ReSpeaker can only cancel audio it knows about. It compares the mic
against the sound it plays itself, so the Pebble V3s must be fed from the ReSpeaker's
3.5 mm output, or the system's output must go through the ReSpeaker. Speakers plugged
straight into the laptop won't be cancelled.

## The pieces

| Piece | Where | What it does |
|-------|-------|--------------|
| The switch | `.env`: `APPY_MIC_HEARS_MUSIC=1`; `hearing_workaround.py` | Turns on the two code rules below, and only while music is playing (Riff or Appy's player). Unset gives the old behaviour. |
| Strict music commands | `tools.py` (`music_call`, `STRICT_MUSIC_MAX_WORDS`), `main.py` (`strict_music=`) | While the switch is on and music plays, a spoken message only forces a music tool if it's at most 6 words. |
| Utterance cap | `hearing.py` (`over_cap`, `Ear(utterance_cap=)`, the capped block in `_vad_loop`), `main.py` (`utterance_cap=`) | While the switch is on and music plays, a spoken turn is closed after 15 s of non-stop "speech". |
| Tests | `tests/test_hearing_workaround.py`, `UtteranceCapTests` in `tests/test_hearing.py`, `test_strict_music_rejects_long_lyrics` in `tests/test_misc.py` | |

## Turning it on

In `.env`: `APPY_MIC_HEARS_MUSIC=1`. Keep `MIC_DEVICE` on the plain laptop mic.

**Tried and dropped: software echo cancellation.** PipeWire's
`libpipewire-module-echo-cancel` (WebRTC) on the laptop mic made things much worse. Recorded
side by side with music playing, Whisper got "can you pause it" 5/5 times from the raw mic
and 0/5 from the echo-cancelled one, which mostly turned speech into near-silence and
garble ("I can pause it, I can pause it", "Thank you."). With the music ducked while Tudor
talks, the raw mic is good enough until the ReSpeaker arrives.

## Removing it (once the ReSpeaker is in)

1. In `.env`, set `MIC_DEVICE` to the ReSpeaker's source (or clear it), and delete
   `APPY_MIC_HEARS_MUSIC`.
2. Delete `hearing_workaround.py`, `tests/test_hearing_workaround.py` and this doc.
3. Remove every remaining tagged spot (the grep above):
   - `hearing.py`: `over_cap`, the `utterance_cap` parameter and attribute, `cap`, and
     the capped block in `_vad_loop`
   - `main.py`: the import, `utterance_cap=` and `strict_music=`
   - `tools.py`: `STRICT_MUSIC_MAX_WORDS`, the `strict` parameter of `music_call` and its
     check, and the `strict_music` parameter of `forced_call`
   - `.env.example`
   - the tagged tests in `tests/test_hearing.py` and `tests/test_misc.py`
4. Drop the pointer to this doc from `CLAUDE.md` and the README's Music section.
5. Run the tests.

**Keep:** ducking (`dj.Ducker`). Turning the music down while Tudor or Appy talks is part
of DJ mode, not of this workaround.
