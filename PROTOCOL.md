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
| `context`    | `tokens`, `limit`                        | How much of the context the conversation uses; `limit` is `COMPACT_AT`, where it's compacted automatically. The compact ring on the input pill fills clockwise from 12 o'clock towards it. Sent at startup, after every turn, after every compaction and after a press of the ring; replayed to UIs that connect later. |
| `widget`     | `widget` (kind) + that kind's fields     | A card Appy pops out next to himself. Kinds: `weather` with `city`, `temp_c` (the high, for a forecast day), optional `low_c` and `when` (short weekday, forecast days only), `condition`, `icon` (`clear`, `clear_night`, `partly_cloudy`, `partly_cloudy_night`, `cloudy`, `fog`, `drizzle`, `rain`, `snow`, `thunder`; unknown names draw as a cloud). Sent when `get_weather` runs; the card flies back into Appy after ~14s or when clicked. |
| `memory_proposals` | `items`: list of `{slug, title, summary, before, after, stale, error}` | Everything the consolidator learned that waits for review (the whole list each time; empty means none). `before`/`after` are the full note; `stale` means the note changed and the proposal is being redone; `error` is why the last decision on it failed. Sent on connect (replayed like `status`), after every decision, and when the 60s poll sees a change. The overlay shows them one per page in the memory panel above the chat. |
| `memory_committed` | `kind` (`remember`, `forget`, `approved`, `edited`, `undo`, `auto`), `summary`, `facts` and `dropped` (optional lists: the sentences the change added and removed), `commit` (optional: the change's commit) | Something was just written to long-term memory, or undone. `auto` is a commit the Mac made with auto-approve on (seen by the 60s poll). Changes within 4 s of each other (with nothing else in the chat between them) share one centred pill: "Remembered 3 facts", "Forgot a fact", "Undid a change". Hovering it unfolds the facts (removed ones struck through; the summary when a change has none). Its Undo sends one `memory_undo` naming the pill's commits (one plain `memory_undo` per change when a change has no commit); once clicked, the pill takes no more changes. Only the newest pill's Undo is live, and an "Undid" pill offers "Undo earlier". |
| `memory_error` | `text` | A memory action failed (e.g. an undo refused because something newer changed memory, or the Mac unreachable). Shown as a notice pill in the chat. |
| `memory_settings` | `auto_approve` (bool) | The memory service's settings. Sent on connect (replayed) and after every change; the overlay passes it to the panel pill. |

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
| `compact`    | —        | The input pill's ring: fold everything but the latest exchange into the session summary now. Queued like a turn, so it runs after the current answer; the ring stays lit until the next `context` arrives, and the UI follows the `compacting` status. |
| `memory_decision` | `slug`, `decision` (`accept`, `deny`, `edit`), `text` and `before` (edit only: the full edited note, and the note as the panel showed it when editing started) | A button on the memory panel (Approve all sends one `accept` per proposal). The backend forwards it to the memory service; code applies it, never the LLM. An edit is refused if the note changed since `before` (so a fact remembered meanwhile isn't overwritten); the panel keeps the typed text and shows the error. An edit of a proposal whose note changed before editing started is carried over to the note as it is now (facts remembered since are kept). |
| `memory_auto_approve` | `on` (bool) | The panel pill's "Auto-approve memories" item. The backend sets it on the Mac and answers with `memory_settings` (the Mac's real value, also when the change failed). |
| `memory_undo` | `commits` (optional: the changes to undo, newest first) | A memory pill's Undo: revert the newest memory change that hasn't been undone. Only the newest pill's button is live, and each works once; the "Undid" pill's "Undo earlier" goes further back. Undoing an accepted or edited proposal puts it back in the panel; undoing an `auto` one just reverts it. With `commits`, the backend undoes exactly those (`POST /undo {"expect": sha}` per commit) and stops with a `memory_error` if something newer changed memory in between. |

## Panel pill bridge (HTTP, 127.0.0.1:8002)

Served by the overlay (`appy-ui/src/net/pill_bridge.rs`) for the Plasma applet.

The POST routes need an `X-Appy-Pill: 1` header and answer `{"ok": false}` without it, so a web page can't use them (a cross-origin request with a custom header needs a CORS preflight, which the bridge never approves).

| Request        | Response |
|----------------|----------|
| `GET /state`   | `{"state": …, "expression": …, "msg_count": n, "minimized": bool, "auto_approve": bool \| null}`; the pill polls this every 200ms. `auto_approve` is null until the backend sends `memory_settings` (or with memory off), which hides the pill's toggle |
| `POST /toggle` | Show/hide the overlay. `{"ok": true}` |
| `POST /quit`   | Quit the overlay. `{"ok": true}` |
| `POST /auto-approve` | Flip auto-approve (sends `memory_auto_approve` with the opposite of the current setting; nothing while it's unknown). `{"ok": true}` |
