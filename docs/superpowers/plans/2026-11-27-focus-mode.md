# Focus Mode State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Focus mode stays on across turns until Tudor asks Appy to switch back.

**Architecture:** `tools.py` holds the current mode and a `set_mode` tool the model calls.
`main.py` stamps every user message with time + mode (`[10:32 PM · FOCUS] ...`), so the
system prompt (and its preheated KV cache) never changes. `prompt.txt` explains the stamp.

**Tech Stack:** Python 3, unittest, OpenAI-compatible tool calling (LM Studio / Gemma).

**Spec:** `docs/superpowers/specs/2026-11-27-focus-mode-design.md`

## Global Constraints

- Modes are exactly `banter` and `focus`; every launch starts in `banter`.
- The system prompt text must not change at runtime.
- Stamp format: `[{time} · {MODE}] {text}`, e.g. `[10:32 PM · FOCUS] what's the pin?` (middle dot U+00B7, mode upper-case).
- Console log on change: `[mode -> focus]`.
- No UI/protocol changes.

## Review Focus

1. Model passes a bad or oddly-cased mode (`"FOCUS"`, `"normal"`, missing) → upper/lower case accepted; unknown value returns a JSON error and leaves the mode unchanged.
2. Setting the mode it's already in → no error, result says it's already that mode.
3. History compaction drops the old `set_mode` call → mode still holds, because it lives in Python and every stamp carries it.
4. `set_mode` must not go in `STALE_TOOLS` (its result isn't stale data, and there is no card).
5. Tool errors never raise into the turn (covered by the existing `run_tool` wrapper).

---

### Task 1: Mode state, `set_mode` tool, and the message stamp

**Files:**
- Modify: `tools.py` (add next to `TOOL_SCHEMAS` / `TOOL_MAP`)
- Test: `tests/test_misc.py` (new `ModeTests` class)

**Interfaces:**
- Produces:
  - `tools.VALID_MODES: tuple[str, ...] = ("banter", "focus")`
  - `tools.current_mode() -> str`
  - `tools.set_mode(mode: str) -> str` — JSON string. Success: `{"mode": "focus", "note": "Mode is now FOCUS. Reply in focus mode."}`; already set: same shape with note `"Already in FOCUS mode."`; unknown: `{"error": "Unknown mode: normal. Use banter or focus."}`. Prints `[mode -> focus]` only when it changes.
  - `tools.reset_mode()` — back to `banter` (for tests).
  - `tools.stamp_message(text: str, now: str) -> str` — `f"[{now} · {current_mode().upper()}] {text}"`.
  - A `set_mode` entry in `TOOL_SCHEMAS` (param `mode`, `enum: ["banter", "focus"]`, required) and `TOOL_MAP["set_mode"] = set_mode`. Description: call only when Tudor asks to change how Appy works with him (e.g. "lock in", "focus up", "ok we're done", "relax"), not for ordinary serious questions.

- [ ] **Step 1: Write the failing tests** (`setUp` calls `tools.reset_mode()`)

```python
def test_starts_in_banter(self):
    self.assertEqual(tools.current_mode(), "banter")

def test_set_mode_persists(self):
    json.loads(tools.run_tool("set_mode", '{"mode": "focus"}'))
    self.assertEqual(tools.current_mode(), "focus")
    self.assertEqual(tools.stamp_message("next one", "10:32 PM"), "[10:32 PM · FOCUS] next one")

def test_case_insensitive(self):
    self.assertEqual(json.loads(tools.set_mode("FOCUS"))["mode"], "focus")

def test_unknown_mode_is_an_error_and_keeps_mode(self):
    tools.set_mode("focus")
    self.assertIn("Unknown mode", json.loads(tools.set_mode("normal"))["error"])
    self.assertEqual(tools.current_mode(), "focus")

def test_missing_mode_is_an_error(self):
    self.assertIn("error", json.loads(tools.run_tool("set_mode", "{}")))

def test_already_in_mode(self):
    self.assertIn("Already", json.loads(tools.set_mode("banter"))["note"])

def test_schema_registered_and_not_stale(self):
    names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
    self.assertIn("set_mode", names)
    self.assertNotIn("set_mode", tools.STALE_TOOLS)

def test_banter_stamp(self):
    self.assertEqual(tools.stamp_message("hi", "09:00 AM"), "[09:00 AM · BANTER] hi")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_misc.py -k Mode -v`
Expected: FAIL (`AttributeError: module 'tools' has no attribute 'reset_mode'`)

- [ ] **Step 3: Implement the Interfaces above in `tools.py`** — keep state in a module-level dict like `main.py`'s `effort_state`; `run_tool`'s existing `TypeError` handling covers a missing `mode` arg.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests -q`
Expected: all pass

- [ ] **Step 5: Commit** `tools.py tests/test_misc.py` — `feat: set_mode tool and persistent banter/focus mode`

### Task 2: Wire the stamp into turns and update the prompt

**Files:**
- Modify: `main.py:117-121` (the `run_turn` call)
- Modify: `prompt.txt` (mode rules, "Current State" block, expression rule)
- Modify: `PROTOCOL.md` only if it documents the user-message prefix (it doesn't today — skip), `README.md:4` ("can check the weather" → mention focus mode)

**Interfaces:**
- Consumes: `tools.stamp_message(text, now)`

- [ ] **Step 1:** In `main.py`, replace `f"[{now}] {user_input}"` with `tools.stamp_message(user_input, now)`.
- [ ] **Step 2:** In `prompt.txt`, remove the `Current State: / - Mode: BANTER` block. Replace the mode bullet's lead-in with: each message starts with `[time · MODE]`; that tag is the current mode; stay in it every turn until Tudor asks to switch; call `set_mode` only when he does, never on your own and never announce the switch robotically. Add under FOCUS MODE: usually use the `[focused]` expression.
- [ ] **Step 3: Verify** `python -m pytest tests -q` passes and `python -c "import main"` imports cleanly.
- [ ] **Step 4: Live check** (if LM Studio is up): run `python main.py`, type "lock in, I'm debugging the arm", then "what's 12 times 7?" — second reply is terse and console shows `[mode -> focus]` once. Then "ok we're done, relax" → `[mode -> banter]`.
- [ ] **Step 5: Commit** `main.py prompt.txt README.md` — `feat: focus mode persists across turns`
