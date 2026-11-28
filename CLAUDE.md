# Appy

A voice-first desktop assistant: Python brain (`main.py`, `agent.py`, `tools.py`, `hearing.py`,
`voice.py`), Rust/GTK4 overlay (`appy-ui/`), Plasma panel pill (`appy-pill/`). README.md has
the overview, PROTOCOL.md the messages between the parts.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -t .
cd appy-ui && cargo test
```

## Temporary hearing workaround

There's a temporary workaround in place until the ReSpeaker mic array arrives, for the
laptop mic hearing the music. Read docs/hearing-workaround.md before touching
`hearing.py`, the music forced calls in `tools.py`, or `MIC_DEVICE`. Every piece of it is
tagged `HEARING-WORKAROUND(respeaker)`.
