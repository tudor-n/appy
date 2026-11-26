# Appy

A voice-first desktop assistant for KDE Plasma (Wayland). A small mascot lives on
your screen, listens, talks back, and can check the weather.

```
 mic ─▶ hearing.py ─┐                                  ┌─▶ appy-ui   mascot + chat overlay (Rust, GTK4 layer-shell)
                    ├─▶ main.py / agent.py ─ ws:8001 ──┤
 typed text ────────┘     │        │                   └─  appy-pill  taskbar pill (Plasma QML) ─ http:8002 ─▶ appy-ui
                          ▼        ▼
                       LLM API   voice.py ─▶ speakers
```

- **Brain** (Python): `hearing.py` does voice detection (Silero), end-of-turn detection
  (Smart Turn) and speech-to-text (faster-whisper). `agent.py` streams replies from any
  OpenAI-compatible server (LM Studio, Ollama, …) and runs tools from `tools.py`.
  `voice.py` speaks sentence by sentence with Kokoro, so speech starts before the
  reply finishes.
- **Body** (`appy-ui/`): a transparent overlay you can drag around. Hover the mascot to
  see the chat, double right-click to pin it open, double left-click to hide it.
- **Pill** (`appy-pill/`): a Plasma panel applet that shows Appy's state and
  toggles the overlay.

The messages between them are documented in [PROTOCOL.md](PROTOCOL.md).

## Running

```sh
# Brain
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # point BASE_URL/MODEL at your LLM server
$EDITOR prompt.txt              # system prompt (gitignored)
# kokoro-v1.0.onnx and voices-v1.0.bin go in the repo root (see kokoro-onnx docs)
.venv/bin/python main.py

# Overlay
cd appy-ui && cargo run --release

# Panel pill (once)
kpackagetool6 -t Plasma/Applet -i appy-pill
```

To work on the UI without the audio stack or an LLM, run `dev/mock_backend.py` instead
of `main.py`.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -t .
cd appy-ui && cargo test
```

## Session logs and compaction

Each run writes a JSONL log of the conversation to `~/.local/share/appy/logs/<session>.jsonl`
(override with `APPY_LOG_DIR`). When a run's context passes `COMPACT_AT` tokens (default 9500),
older turns are folded into a summary between turns and the UI shows "compacting…".
Set `APPY_DUMP_COMPACTION=1` to also write each compaction's exact prompt and the summary that
came back to `<session>.compaction-N.txt` next to the log; `COMPACT_AT=2500` makes it happen
sooner when testing.
