# Focus mode as a persistent state

## Why
Asking Appy to focus only changes one reply. `prompt.txt` hard-codes `Mode: BANTER`, so the
next turn reads that again and drifts back. Focus mode should stick until Tudor ends it.

## Goals / non-goals
- Goal: the mode (`banter` | `focus`) persists across turns until Tudor asks to switch.
- Goal: Appy decides when to switch, from natural phrasing ("lock in", "ok we're done").
- Non-goal: UI indicator (overlay/pill). Possible follow-up; needs protocol + Rust changes.
- Non-goal: persisting the mode across restarts. Every launch starts in banter.
- Anti-goal: invalidating the preheated system-prompt cache on every switch.

## Approaches
- **Model-driven `set_mode` tool (picked by Tudor).** Handles any phrasing; costs one extra tool
  round on the switching turn.
- Python phrase detection: instant, but misses wordings.
- UI toggle: left for later.
- Rewriting the system prompt per switch: rejected, breaks the KV cache.

## Design
- Python owns `mode`, default `banter`.
- New tool `set_mode(mode: "banter" | "focus")`. Instant; result tells the model the new mode
  and to reply in it, so the same turn's reply already uses the new mode.
- Each user message's stamp gains the mode: `[10:32 PM · FOCUS]` (was `[10:32 PM]`).
- `prompt.txt`: drop the hard-coded "Current State: Mode: BANTER"; explain the stamp is the
  current mode, stay in it until Tudor asks to switch, call `set_mode` only then. In focus mode,
  lean towards the `[focused]` expression (still the model's pick).
- Console logs `[mode -> focus]`.
- Tests: tool flips state; stamp includes mode; the turn after a switch still carries FOCUS.

## Left to the builder
Where the mode state lives (tools module vs. main), exact prompt wording.
