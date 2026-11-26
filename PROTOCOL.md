# Appy wire protocol

Three processes talk over localhost:

```
 Python brain (main.py)  ──WebSocket :8001──▶  appy-ui overlay (Rust/GTK)  ◀──HTTP :8002──  appy-pill (Plasma QML)
```

The Python side is the WebSocket **server** (`ui_bridge.py`); the overlay connects as a
client and reconnects every 2s if the backend is down. Every message is a JSON object
with a `type` field.

The Rust types live in `appy-ui/src/protocol.rs`. `tests/test_protocol.py` fails if the
Python and Rust lists of expressions, efforts or states drift apart.

## Backend → UI

| `type`       | Fields                                   | Meaning |
|--------------|------------------------------------------|---------|
| `wake`       | —                                        | The mic heard speech start. The mascot switches to its listening face. |
| `transcript` | `text`, `is_final` (bool)                | What the user said. `is_final: false` is reserved for live partial transcripts (shown in the input field); the backend currently only sends final ones. |
| `status`     | `state`, `label`                         | See states below. Busy states (`booting`, `thinking`, `compacting`) add an animated pill to the chat, e.g. "thinking..." → "thought for 3s". |
| `response`   | `text`, `expression` (optional)          | A reply bubble. `expression` sets the mascot's face. |
| `effort`     | `value`                                  | Current reasoning effort; sent on connect and after every change so all UIs agree. |
| `widget`     | `widget` (kind) + that kind's fields     | A card Appy pops out next to himself. Kinds: `weather` with `city`, `temp_c` (the high, for a forecast day), optional `low_c` and `when` (short weekday, forecast days only), `condition`, `icon` (`clear`, `clear_night`, `partly_cloudy`, `partly_cloudy_night`, `cloudy`, `fog`, `drizzle`, `rain`, `snow`, `thunder`; unknown names draw as a cloud). Sent when `get_weather` runs; the card flies back into Appy after ~14s or when clicked. |
| `memory_proposals` | `items`: list of `{slug, title, summary, before, after, stale, error}` | Everything the consolidator learned that waits for review (the whole list each time; empty means none). `before`/`after` are the full note; `stale` means the note changed and the proposal is being redone; `error` is why the last decision on it failed. Sent on connect (replayed like `status`), after every decision, and when the 60s poll sees a change. The overlay shows a collapsed "N memory updates" card above the input. |
| `memory_committed` | `kind` (`remember`, `forget`, `approved`, `edited`, `undo`), `summary` | Something was just written to long-term memory, or undone. The overlay adds a chip to the chat ("Remembered: …", "Saved: …", "Undid: …"); all but `undo` have an Undo button. |

**States:** `idle`, `listening`, `booting`, `thinking`, `compacting`, `speaking`.
On the Python side `StatusTracker` (`status.py`) is the only thing that sends
`thinking`/`speaking`/`idle`. It combines the turn phase from the agent with whether the
speaker still has text queued or playing. `booting` is sent with `StatusTracker.announce`.
`compacting` is a tracker phase, set while older turns are summarised between turns.
After a `wake`, the tracker never sends `idle` until the user's turn starts (or the speech is
discarded as noise), so a late end-of-audio or end-of-compaction can't cancel "listening".
The bridge replays the last `status` (after `effort`) to every UI that connects, so an overlay
that connects mid-boot still shows "loading…" and the mascot's loading bar.

**Expressions:** `neutral`, `happy`, `excited`, `curious`, `surprised`, `focused`,
`skeptical`. The LLM picks one by starting its reply with a tag like `[happy]`, which
`ExpressionTagParser` strips before the text is shown or spoken.

## UI → backend

| `type`       | Fields   | Meaning |
|--------------|----------|---------|
| `user_text`  | `text`   | A typed message. Queued like a spoken one. |
| `set_effort` | `effort` | One of `none`, `low`, `medium`, `high` (passed to the LLM as `reasoning_effort`). |
| `compact`    | —        | The input pill's circle button: fold everything but the latest exchange into the session summary now. Queued like a turn, so it runs after the current answer; the UI follows the `compacting` status. |
| `memory_decision` | `slug`, `decision` (`accept`, `deny`, `edit`), `text` (edit only: the full note) | A button on the memory card. The backend forwards it to the memory service; code applies it, never the LLM. |
| `memory_undo` | — | A chip's Undo button: revert the newest memory change that hasn't been undone. Pressing again goes further back. |

## Panel pill bridge (HTTP, 127.0.0.1:8002)

Served by the overlay (`appy-ui/src/net/pill_bridge.rs`) for the Plasma applet.

| Request        | Response |
|----------------|----------|
| `GET /state`   | `{"state": …, "expression": …, "msg_count": n, "minimized": bool}`; the pill polls this every 200ms |
| `POST /toggle` | Show/hide the overlay. `{"ok": true}` |
| `POST /quit`   | Quit the overlay. `{"ok": true}` |
