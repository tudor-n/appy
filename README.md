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

# Panel pill (once; use -u instead of -i to update it, then restart plasmashell)
kpackagetool6 -t Plasma/Applet -i appy-pill
```

Or start both together with `bin/appy`: it rebuilds the overlay if needed, and Ctrl+C,
saying "quit", or quitting from the pill stops everything. Put it on your PATH with
`ln -s "$PWD/bin/appy" ~/.local/bin/appy`.

The panel pill is always in the panel. While Appy is off it shows him asleep; clicking it
runs `~/.local/bin/appy --no-build` (so the symlink above is needed, and the overlay must
have been built once), logging to `~/.cache/appy/appy.log`. Appy pops out showing a
loading bar until he's ready; after that a click shows or hides him.

To work on the UI without the audio stack or an LLM, run `dev/mock_backend.py` instead
of `main.py`.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -t .
cd appy-ui && cargo test
```
