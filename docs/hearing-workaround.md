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
| Echo-cancelled mic | `audio/appy-echo-cancel.conf`, `bin/appy-echo-cancel` | PipeWire `libpipewire-module-echo-cancel` (WebRTC) on the laptop mic, with whatever the speakers play as the reference. Shows up as the source `appy_echo_cancel_source`; Appy uses it through `MIC_DEVICE`. |
| The switch | `.env`: `APPY_MIC_HEARS_MUSIC=1`; `hearing_workaround.py` | Turns on the two code rules below, and only while Riff is playing. Unset gives the old behaviour. |
| Strict music commands | `tools.py` (`music_call`, `STRICT_MUSIC_MAX_WORDS`), `main.py` (`strict_music=`) | While the switch is on and Riff plays, a spoken message only forces a music tool if it's at most 6 words. |
| Utterance cap | `hearing.py` (`over_cap`, `Ear(utterance_cap=)`, the capped block in `_vad_loop`), `main.py` (`utterance_cap=`) | While the switch is on and Riff plays, a spoken turn is closed after 15 s of non-stop "speech". |
| Tests | `tests/test_hearing_workaround.py`, `UtteranceCapTests` in `tests/test_hearing.py`, `test_strict_music_rejects_long_lyrics` in `tests/test_misc.py` | |

## Turning it on

```sh
bin/appy-echo-cancel install      # restarts PipeWire
# .env:
MIC_DEVICE=appy_echo_cancel_source
APPY_MIC_HEARS_MUSIC=1
```

## Removing it (once the ReSpeaker is in)

1. `bin/appy-echo-cancel remove` (deletes the PipeWire config and restarts PipeWire).
2. In `.env`, set `MIC_DEVICE` to the ReSpeaker's source (or clear it), and delete
   `APPY_MIC_HEARS_MUSIC`.
3. Delete `audio/appy-echo-cancel.conf`, `bin/appy-echo-cancel`, `hearing_workaround.py`,
   `tests/test_hearing_workaround.py` and this doc.
4. Remove every remaining tagged spot (the grep above):
   - `hearing.py`: `over_cap`, the `utterance_cap` parameter and attribute, `cap`, and
     the capped block in `_vad_loop`
   - `main.py`: the import, `utterance_cap=` and `strict_music=`
   - `tools.py`: `STRICT_MUSIC_MAX_WORDS`, the `strict` parameter of `music_call` and its
     check, and the `strict_music` parameter of `forced_call`
   - `.env.example`
   - the tagged tests in `tests/test_hearing.py` and `tests/test_misc.py`
5. Drop the pointer to this doc from `CLAUDE.md` and the README's Music section.
6. Run the tests.

**Keep:** ducking (`dj.Ducker`). Turning the music down while Tudor or Appy talks is part
of DJ mode, not of this workaround.
