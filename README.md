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

## Session logs and compaction

Each run writes a JSONL log of the conversation to `~/.local/share/appy/logs/<session>.jsonl`
(override with `APPY_LOG_DIR`). When a run's context passes `COMPACT_AT` tokens (default 9500),
older turns are folded into a summary between turns and the UI shows "compacting…".
Set `APPY_DUMP_COMPACTION=1` to also write each compaction's exact prompt and the summary that
came back to `<session>.compaction-N.txt` next to the log; `COMPACT_AT=2500` makes it happen
sooner when testing.

## Long-term memory

Durable facts live on the Mac mini, in a git repo at `~/appy-data` (`memory/core.md` plus one
markdown note per topic). The memory service (`appy_memory/`, FastAPI on port 8765) owns every
read and write. At startup Appy puts `core.md` and the note index into its system prompt; the
model can then call `memory_read`, `memory_remember` and `memory_forget`. Each change is a commit
whose message carries the session id. When Appy quits, it uploads its session log to
`~/appy-data/logs/` and deletes the local copy; logs that couldn't be uploaded go up at the next
start. If the Mac is unreachable, Appy starts without long-term memory.

Laptop `.env`: `MEMORY_URL=http://100.103.162.57:8765` and `MEMORY_TOKEN=<shared token>`.
Mac `~/appy-memory/.env`: `MEMORY_TOKEN=<same token>`, `MEMORY_DIR=~/appy-data`,
`MEMORY_HOST=0.0.0.0`, `MEMORY_PORT=8765`.

```sh
appy_memory/deploy/deploy.sh        # copy to the Mac, run its tests, (re)start under launchd
ssh macmini tail -f appy-memory/service.log         # one line per remember/forget/undo/upload
ssh macmini git -C appy-data log --stat             # history of every memory change
curl -X POST -H "Authorization: Bearer $MEMORY_TOKEN" $MEMORY_URL/undo   # undo the last change
.venv/bin/python dev/check_memory_tools.py          # live check that the model calls the tools
```

Set `APPY_DUMP_PROMPT=1` to write the exact system prompt of a run to `<session>.prompt.txt` next
to its log.

### Learning from conversations (the consolidator)

You don't have to say "remember". After a session's log reaches the Mac, the memory service
reads it in the background: it cuts the transcript into ~5k-token windows, asks the model which
notes each window adds durable facts to (project details, decisions, preferences; never small
talk), and rewrites those notes with the new facts folded in. Code checks every rewrite (title and
gist kept, bullets only, under the size cap, actually changed). The result waits as a proposal in
`~/appy-data/pending/<note>.json`, one per note, for you to approve in the overlay; nothing reaches
memory without that (unless auto-approve is on, below). While you're talking to Appy the consolidator waits (the laptop pings
`/activity` at each turn), so it never competes with live chat for the model.

Mac `~/appy-memory/.env` additions: `BASE_URL=http://100.103.162.57:1234/v1` (LM Studio listens on
the Tailscale IP only), `MODEL=google/gemma-4-26b-a4b`, optionally `CONSOLIDATOR_EFFORT` (default
`none`: same quality as `low` here, 15x faster), `CONSOLIDATOR_WINDOW_TOKENS` (5000),
`CONSOLIDATOR_IDLE_SECONDS` (120), and `CONSOLIDATOR_DUMP=1` to write every prompt and output to
`~/appy-data/dumps/`.

```sh
ssh macmini grep consolidator appy-memory/service.log    # one line per session processed
curl -H "Authorization: Bearer $MEMORY_TOKEN" $MEMORY_URL/proposals   # what's waiting for review
curl -H "Authorization: Bearer $MEMORY_TOKEN" $MEMORY_URL/settings    # {"auto_approve": …}
.venv/bin/python dev/check_consolidator.py 2             # live check on 7 fixture sessions
```

### Reviewing what Appy learned

Proposals show up in a memory panel that floats above the chat while the chat is open (below
it when Appy sits near the top of the screen). It shows one note at a time: the title, what
changed (removed lines struck through, new ones in white) and **Approve**, **Deny** and **Edit**
(Edit opens the whole note as text; Save checks it keeps the title, the description line and
"- " bullets). Scroll over the panel, or use ▲ ▼, for the next one ("2 of 3"). **Approve all**
approves every note that isn't being edited or redone; **Later** hides the panel until something
new arrives (or the next launch), without deciding anything. If the note changed since the
proposal was written, Approve shows "updating…" while the Mac redoes it against the current
note; review it again when it comes back. While a proposal is being redone it can still be
denied or edited. The panel refreshes when Appy starts, after each decision, and every minute.

Every change to memory (a remember, a forget, an approved, edited or auto-approved proposal)
adds a chip to the chat, like "Saved: Added the helmet HUD hardware", with **Undo**, which
reverts the newest change (only the newest chip's Undo is live); the "Undid" chip that follows
offers "Undo earlier" to go further back. Undoing an approved proposal puts it back in the
panel; undoing an auto-approved one just reverts it.

Appy knows about every change right away: his memory in the system prompt is built once at
startup (so the prompt stays cached), and changes made during the session ride along with your
next message as a short note with the facts themselves, like
`[Memory changed since your last reply: note 'bench' now also says: The PSU is a Korad
KA3005P.; no longer says: The PSU is a Riden RD6024.]`.

**Auto-approve.** Right-click the panel pill → **Auto-approve memories** (shown while Appy runs).
With it on, nothing waits for review: what the consolidator learns is committed straight away
(still through the same checks) as `memory: … [auto]`, and an "Auto-saved: …" chip with Undo
shows up if Appy is running. Turning it on also approves what's already waiting. The setting
lives on the Mac (`~/appy-data/settings.json`), so it holds across launches and covers sessions
processed while the laptop is off.

`dev/mock_backend.py --memory --busy` sends three sample proposals and answers the panel and
the auto-approve toggle, for working on the panel without the Mac (`--busy` keeps the chat
open; `MOCK_PORT` and the overlay's `APPY_WS_URL` / `APPY_PILL_ADDR` run it beside a real Appy).
