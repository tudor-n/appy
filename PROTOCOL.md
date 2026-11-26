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

**States:** `idle`, `listening`, `booting`, `thinking`, `compacting`, `speaking`.
On the Python side `StatusTracker` (`status.py`) is the only thing that sends
`thinking`/`speaking`/`idle`. It combines the turn phase from the agent with whether the
speaker still has text queued or playing. `booting` is sent with `StatusTracker.announce`. The UI also
understands `compacting`, but the backend doesn't send it: trimming history is instant.

**Expressions:** `neutral`, `happy`, `excited`, `curious`, `surprised`, `focused`,
`skeptical`. The LLM picks one by starting its reply with a tag like `[happy]`, which
`ExpressionTagParser` strips before the text is shown or spoken.

## UI → backend

| `type`       | Fields   | Meaning |
|--------------|----------|---------|
| `user_text`  | `text`   | A typed message. Queued like a spoken one. |
| `set_effort` | `effort` | One of `none`, `low`, `medium`, `high` (passed to the LLM as `reasoning_effort`). |

## Panel pill bridge (HTTP, 127.0.0.1:8002)

Served by the overlay (`appy-ui/src/net/pill_bridge.rs`) for the Plasma applet.

| Request        | Response |
|----------------|----------|
| `GET /state`   | `{"state": …, "expression": …, "msg_count": n, "minimized": bool}`; the pill polls this every 200ms |
| `POST /toggle` | Show/hide the overlay. `{"ok": true}` |
| `POST /quit`   | Quit the overlay. `{"ok": true}` |
