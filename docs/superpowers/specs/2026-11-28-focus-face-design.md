# Focused face as the focus-mode indicator

## Why
In focus mode only the first reply showed the focused face; Gemma picked neutral for the
rest, and the overlay relaxes to neutral when the chat is put away. Tudor wants the focused
face to *be* the indicator of the current mode.

## Design (approved: "go with both")
- **Replies:** in focus mode every reply carries `expression: "focused"`, whatever Gemma
  tagged. `tools.pinned_expression()` returns `"focused"` in focus mode, else `None`;
  `Agent(pinned_expression=...)` applies it to each round's expression (also what's logged).
  Error replies keep their own face.
- **Resting face:** new backend → UI message `{"type": "mode", "value": "banter"|"focus"}`,
  sent on connect (like `effort`) and whenever `set_mode` runs. The overlay stores the mode;
  its resting face is `focused` in focus mode, `neutral` in banter. A mode message sets the
  face immediately; putting the chat away relaxes to the resting face. The pill follows.
- Listening/thinking/talking animations still play; banter keeps Gemma's own faces.
- `tests/test_protocol.py` keeps the Rust `Mode` enum in sync with `tools.VALID_MODES`.

## Approaches
- Backend-only pin: rejected, the face drops to neutral when the chat closes.
- Overlay-only (ignore reply faces in focus mode): logs and pill would disagree with the face.
