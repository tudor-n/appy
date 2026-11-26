# Memory Plan 1: Session Logs & Live Compaction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every run of Appy writes a crash-safe JSONL log of the conversation. Long runs stay inside the 16k context by summarising older turns instead of dropping them.

**Architecture:** Two new flat modules on the laptop side, following the repo's style of one module per concern at the root: `session_log.py` (the append-only logger) and `compaction.py` (pure split and render functions plus one summariser call). `Agent` gets a logger, measures tokens from the server's usage chunk, and exposes `maybe_compact()`, which `main.py` calls after each turn's speech finishes. `compact_history` is removed.

**Tech Stack:** Python 3, `openai` client (LM Studio, OpenAI-compatible), `unittest`, the existing fakes in `tests/fakes.py`.

**Spec:** `docs/superpowers/specs/2026-11-26-memory-design.md` (sections A and B and build-order phases 1–2). Phases 3–5 (Mac memory service, consolidator, UI review) are separate later plans.

**Branch:** `feat/memory-logs-compaction`, created from the current `feat/responsive-overlay` HEAD. It needs `on_tool_result` and `ALREADY_SAID_NOTE` from that branch.

## Global Constraints

- The system prompt (`messages[0]`) is never modified or replaced. It stays byte-for-byte the same within a run, to keep the KV cache.
- The message list must never contain two `user` messages in a row (Gemma's template), and an assistant message with `tool_calls` must never be separated from its `tool` results.
- Log location: `APPY_LOG_DIR` env, default `~/.local/share/appy/logs`. File name `<session_id>.jsonl`, with `session_id = YYYY-MM-DD_HHMMSS` in local time.
- Log events: `session_start{model,effort}`, `user{text,source}`, `assistant{text,expression}`, `tool_call{call_id,name,args}`, `tool_result{call_id,name,result}` (result ≤ 4096 chars, `truncated: true` when cut), `compaction{summary}`, `session_end{reason}`. Every line also carries `v: 1`, `seq` (1, 2, 3…) and `ts` (ISO-8601 with UTC offset).
- `user.text` is the raw text, without the `[HH:MM AM]` prefix that `main.py` adds.
- Logging must never break a turn or startup. Write errors are printed once and then swallowed.
- Compaction config (env, with these defaults): `COMPACT_AT=9500`, `TAIL_BUDGET=2500`, `SUMMARY_MAX=700`. The summariser call uses `temperature=0.2`, `reasoning_effort="none"`, `max_tokens=SUMMARY_MAX+100`.
- The rebuilt history is `[system, {"role":"user","content": SUMMARY_PREFIX + summary}, {"role":"assistant","content":"Got it."}, *tail]`, with `SUMMARY_PREFIX = "[Earlier in this session — summary]\n"`.
- Token estimate: `ceil(len(text) / 3.2)`.
- The summary template is the spec's, given to the model exactly as written.

## Review Focus

1. **LM Studio sends no usage chunk.** Compaction must still trigger, from the estimate. Tested in Task 3 (`test_context_tokens_falls_back_to_estimate`).
2. **The summariser fails or returns blank** (LLM outage, empty output). The conversation must stay valid and shrink anyway. Tested in Task 4 (`test_failed_summary_still_shrinks`, `test_blank_summary_counts_as_failure`).
3. **A single huge last turn bigger than `TAIL_BUDGET`.** The tail must still start at that turn's user message and keep the whole turn. Tested in Task 4 (`test_tail_keeps_whole_last_turn_even_if_over_budget`).
4. **Log directory unwritable** (bad env path, full disk). Appy must keep answering. Tested in Task 1 (`test_unwritable_dir_never_raises`).
5. **Romanian diacritics and emoji in user text.** They must be stored as UTF-8, not `\u` escapes, so later stages read them cleanly. Tested in Task 1 (`test_non_ascii_is_kept_readable`).

---

### Task 1: SessionLogger

**Files:**
- Create: `session_log.py`
- Test: `tests/test_session_log.py`

**Interfaces:**
- Produces:
  - `class SessionLogger(logs_dir: Path, now: Callable[[], datetime] = lambda: datetime.now().astimezone())` with attributes `session_id: str` and `path: Path`, and methods `start(model: str, effort: str)`, `user(text: str, source: str)`, `assistant(text: str, expression: str)`, `tool_call(call_id: str, name: str, args: str)`, `tool_result(call_id: str, name: str, result: str)`, `compaction(summary: str)`, `end(reason: str)`. All of them return `None`.
  - `class NullLogger` with the same methods as no-ops.
  - `default_logs_dir() -> Path`.

- [ ] **Step 1: Write the failing tests** in `tests/test_session_log.py`. Use `tempfile.TemporaryDirectory`, and a fixed `now` returning `datetime(2026, 11, 26, 15, 30, 12, tzinfo=timezone(timedelta(hours=2)))`.
  - `test_session_id_and_path`: `session_id == "2026-11-26_153012"` and `path.name == "2026-11-26_153012.jsonl"`.
  - `test_events_are_one_json_line_each`: call start, user, tool_call, tool_result, assistant, compaction, end. Read the file lines. The types are in that order, `seq` is `[1..7]`, every line has `v == 1`, and `ts` ends with `"+02:00"`.
  - `test_file_is_valid_without_end` (crash): call start and user, never end. Every line parses with `json.loads`, and the last line's `type == "user"`.
  - `test_tool_result_is_truncated`: a 5000-char result is stored as 4096 chars with `truncated is True`. A short result has no `truncated` key.
  - `test_unwritable_dir_never_raises`: `logs_dir` = the path of an existing *file*. `start`, `user` and `end` don't raise.
  - `test_non_ascii_is_kept_readable`: `user("Bună ziua 👋", "voice")`. The raw file text contains `Bună ziua 👋`.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_session_log -v`. Expected: an import error (`No module named 'session_log'`).

- [ ] **Step 3: Implement `session_log.py`.**
  - Create the directory lazily on the first write with `mkdir(parents=True, exist_ok=True)`.
  - Open the file in append mode with `encoding="utf-8"`, and keep it open.
  - Write each event as `json.dumps(obj, ensure_ascii=False)` + `"\n"`, then `flush()`. On `end`, also call `os.fsync` and close.
  - Guard writes with a `threading.Lock`.
  - On `OSError`, print `[log] disabled: <err>` once and turn every later call into a no-op.
  - `default_logs_dir()` returns `Path(os.getenv("APPY_LOG_DIR") or "~/.local/share/appy/logs").expanduser()`.

- [ ] **Step 4: Run** the tests from Step 2. Expected: 6 tests pass.

- [ ] **Step 5: Commit.**
```bash
git add session_log.py tests/test_session_log.py
git commit -m "feat: append-only JSONL session logger"
```

### Task 2: Log the conversation from Agent and main

**Files:**
- Modify: `agent.py` (`Agent.__init__`, `_run_rounds`)
- Modify: `main.py` (`main()`)
- Modify: `tests/fakes.py` (add `RecordingLogger`)
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `SessionLogger`, `NullLogger` and `default_logs_dir` from Task 1.
- Produces: `Agent(..., log=None)`, stored as `self.log` (it falls back to `NullLogger()`). `tests.fakes.RecordingLogger` has the same methods; each call appends `(method_name, kwargs_dict)` to `.events`.

- [ ] **Step 1: Write the failing tests** in `tests/test_agent.py`. `make_agent` gains a `log=None` argument that it passes through.
  - `test_plain_reply_is_logged`: a `[happy] Hello there.` reply gives `log.events == [("assistant", {"text": "Hello there.", "expression": "happy"})]`.
  - `test_tool_turn_is_logged_in_order`: a preamble `"Let me check."` plus a `get_weather` call, with the tool returning `'{"temp_c":3}'`, then `"It's 3 degrees."`. The event names are `["assistant", "tool_call", "tool_result", "assistant"]`. The `tool_result` result is exactly `'{"temp_c":3}'`, without `ALREADY_SAID_NOTE`, and `tool_call` has `call_id="c1"`, `name="get_weather"`, `args="{}"`.
  - `test_failed_turn_logs_nothing_from_agent`: with a `ConnectionError` script, `log.events == []`.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_agent -v`. Expected: the 3 new tests fail (`unexpected keyword argument 'log'`).

- [ ] **Step 3: Implement.**
  - Call logger methods with keyword arguments only, so `RecordingLogger` records matching dicts.
  - In `_run_rounds`: log `assistant` for every non-empty reply that is shown to the user (a preamble before tools, or the final reply), `tool_call` before running each tool, and `tool_result` with the raw result right after.
  - In `main.py`:
    - Create `log = SessionLogger(default_logs_dir())` after the client, call `log.start(model, effort_state["value"])`, and pass `log=log` to `Agent`.
    - Call `log.user(user_input, source)` just before `agent.run_turn`. That is the raw text, and source is `"voice"` or `"text"`.
    - Set `reason = "quit"`. The `KeyboardInterrupt` handler keeps `"quit"`. A new `except Exception: reason = "error"; raise` sets `"error"`.
    - Call `log.end(reason)` first in `finally`.

- [ ] **Step 4: Run** the full suite, `.venv/bin/python -m unittest discover -s tests -t .`. Expected: OK.

- [ ] **Step 5: Commit.**
```bash
git add agent.py main.py tests/fakes.py tests/test_agent.py
git commit -m "feat: log every turn, tool call and reply to the session log"
```

### Task 3: Measure context size

**Files:**
- Modify: `agent.py` (`_stream_round`, new `context_tokens`)
- Create: `compaction.py` (just `estimate_tokens` for now)
- Modify: `tests/fakes.py` (add `usage_chunk`)
- Test: `tests/test_agent.py`, `tests/test_compaction.py`

**Interfaces:**
- Produces:
  - `compaction.estimate_tokens(obj) -> int`, which is `ceil(len(json.dumps(obj, ensure_ascii=False)) / 3.2)` for any JSON-serialisable value.
  - `Agent.last_usage: int | None`, set to `prompt_tokens + completion_tokens` from the last request's usage chunk, or `None`.
  - `Agent.context_tokens() -> int`, which returns `last_usage` if it is set, else `estimate_tokens(self.messages) + estimate_tokens(self.tools)`.
  - `tests.fakes.usage_chunk(prompt_tokens: int, completion_tokens: int)`, a chunk with `choices=[]` and `usage=SimpleNamespace(prompt_tokens=…, completion_tokens=…)`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_compaction.py::EstimateTests.test_estimate_tokens`: `estimate_tokens("a" * 31) == 11`. That's 33 chars including the JSON quotes; 33 / 3.2 rounds up to 11.
  - `test_agent.py::test_usage_chunk_sets_context_tokens`: script `[text_chunk("Hi."), usage_chunk(9000, 50)]`. After the turn, `agent.context_tokens() == 9050`, and `client.calls[0]["stream_options"] == {"include_usage": True}`.
  - `test_agent.py::test_context_tokens_falls_back_to_estimate`: no usage chunk. `agent.last_usage is None`, and `agent.context_tokens() == estimate_tokens(agent.messages) + estimate_tokens(agent.tools)`.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_compaction tests.test_agent -v`. Expected: the new tests fail.

- [ ] **Step 3: Implement.**
  - Add `stream_options={"include_usage": True}` to the request in `_stream_round`.
  - Reset `self.last_usage = None` at the start of each round.
  - In the chunk loop, before the `if not chunk.choices: continue`, read `getattr(chunk, "usage", None)`. The existing fakes have no `usage` attribute, so it has to be `getattr`.

- [ ] **Step 4: Run** the full suite. Expected: OK.

- [ ] **Step 5: Commit.**
```bash
git add agent.py compaction.py tests/fakes.py tests/test_agent.py tests/test_compaction.py
git commit -m "feat: track context size from server usage, with an estimate fallback"
```

### Task 4: Compaction core

**Files:**
- Modify: `compaction.py`
- Modify: `tests/fakes.py` (`completion` helper; `FakeClient` returns non-list scripts as they are)
- Test: `tests/test_compaction.py`

**Interfaces:**
- Consumes: `estimate_tokens` from Task 3.
- Produces:
  - Constants `SUMMARY_PREFIX`, `SUMMARY_ACK = "Got it."`, `SUMMARY_TEMPLATE` (the spec's template, with `{previous}` and `{conversation}` placeholders), `TAIL_BUDGET = 2500`, `SUMMARY_MAX = 700`.
  - `split(messages: list[dict], tail_budget: int) -> tuple[str | None, list[dict], list[dict]] | None` returns `(previous_summary, middle, tail)`, or `None` when there's nothing to fold.
  - `render(messages: list[dict]) -> str`.
  - `compact(client, model: str, messages: list[dict], tail_budget: int = TAIL_BUDGET, summary_max: int = SUMMARY_MAX) -> str | None` rebuilds `messages` in place. It returns the new summary, or `None` if summarising failed.
  - `tests.fakes.completion(text: str)` is `SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])`.

How `split` works, since the tests alone don't pin this down:
```
body starts at 1; if messages[1] is a user message starting with SUMMARY_PREFIX and
messages[2] == assistant SUMMARY_ACK: previous = messages[1] minus prefix, body starts at 3.
Walk back from the end summing estimate_tokens(msg); stop before exceeding tail_budget.
Move the tail start BACK to the nearest user message at or before that point (so the
tail always starts at a user message and always holds at least the whole last turn).
If tail start <= body start: return None.  middle = body[:tail start], tail = rest.
```

How `render` works: `U: <content>` for user messages and `A: <content>` for assistant messages. Assistant `tool_calls` render as `A called <name>(<args>)`, and tool messages as `Tool: <first 200 chars>`. Skip `None` or empty content, and join with `"\n"`.

How `compact` works:
- If `split` returns `None`, return `None` without making a request and leave `messages` alone.
- Otherwise make one non-streamed request: `messages=[{"role": "user", "content": SUMMARY_TEMPLATE.format(previous=prev or "none", conversation=render(middle))}]`, `temperature=0.2`, `reasoning_effort="none"`, `max_tokens=summary_max + 100`.
- On success, replace `messages[1:]` with the summary pair plus the tail.
- If the request raises, or returns blank text, keep the old summary pair (if there was one) plus the tail, and return `None`. The conversation shrinks either way.

- [ ] **Step 1: Write the failing tests** in `tests/test_compaction.py`. Use a helper `turn(i, size)` that builds a user/assistant pair whose content is `"x" * size`.
  - `test_split_returns_none_when_everything_fits`: 3 short turns with a budget of 2500 return `None`.
  - `test_tail_starts_at_user_and_keeps_tool_pairs`: 10 turns of `[user, assistant(tool_calls), tool, assistant]`, each about 400 tokens, with a budget of 1000. `tail[0]["role"] == "user"`, every `tool` in the tail comes after an assistant with `tool_calls` in the tail, and `len(middle) > 0`.
  - `test_tail_keeps_whole_last_turn_even_if_over_budget`: the last turn is 20,000 chars, with a budget of 2500. The tail is exactly that last turn, and its first message is the user message.
  - `test_previous_summary_is_detected`: `messages[1:3]` is a summary pair (`"old facts"`). `split` returns `previous == "old facts"`, and the middle doesn't contain the pair.
  - `test_render`: a user message `"my cat is Pixel"`, then a tool call, then a 500-char tool result. The output contains `"U: my cat is Pixel"`, `"A called get_weather({})"`, and a `Tool:` line no longer than 206 chars.
  - `test_compact_rebuilds_history`: `FakeClient([completion("FACTS: cat is Pixel")])`. The roles after compaction are `["system", "user", "assistant", "user", …]`, `messages[0]` equals the original system dict, `messages[1]["content"] == SUMMARY_PREFIX + "FACTS: cat is Pixel"`, and `messages[2]["content"] == "Got it."`.
  - `test_compact_request_settings`: `calls[0]` has `temperature == 0.2`, `reasoning_effort == "none"` and `max_tokens == 800`. Its prompt contains `"U: my cat is Pixel"` and `"Previous summary:\nnone"`.
  - `test_second_compaction_folds_previous_summary`: on an already-compacted history, the prompt contains the old summary text verbatim, and the result still has exactly one summary pair.
  - `test_failed_summary_still_shrinks`: `FakeClient([ConnectionError()])` makes `compact` return `None`. Afterwards `messages[1]["role"] == "user"`, the result is shorter than before, and no two user messages sit next to each other.
  - `test_blank_summary_counts_as_failure`: `completion("   ")` gives the same outcome as the failure test.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_compaction -v`. Expected: the new tests fail.

- [ ] **Step 3: Implement** `split`, `render` and `compact` in `compaction.py`, as described above. In `FakeClient._create`, return `script` itself when it isn't a list (a non-stream response).

- [ ] **Step 4: Run** the full suite. Expected: OK.

- [ ] **Step 5: Commit.**
```bash
git add compaction.py tests/fakes.py tests/test_compaction.py
git commit -m "feat: summarise older turns into a rolling session summary"
```

### Task 5: Compact after turns, replacing compact_history

**Files:**
- Modify: `agent.py` (remove `compact_history`, `HISTORY_MAX_MESSAGES` and `HISTORY_KEEP_MESSAGES`; add `maybe_compact`; constructor args)
- Modify: `main.py` (read env, call `maybe_compact`)
- Modify: `PROTOCOL.md` (the backend now sends `compacting`)
- Modify: `tests/fakes.py` (`FakeClient` accepts a callable responder)
- Test: `tests/test_agent.py` (delete `CompactHistoryTests` and `test_compaction_happens_before_request`, add new tests)

**Interfaces:**
- Consumes: `compaction.compact` (Task 4), `Agent.context_tokens` (Task 3), `Agent.log` (Task 2), `StatusTracker.announce` / `publish` (`status.py`).
- Produces: `Agent(..., compact_at: int = 9500, tail_budget: int = 2500, summary_max: int = 700)` and `Agent.maybe_compact() -> bool`, which returns `True` if it compacted. `FakeClient(responder=fn)` calls `fn(kwargs)` for each request instead of popping a script.

- [ ] **Step 1: Write the failing tests** in `tests/test_agent.py`.
  - `test_maybe_compact_noop_under_threshold`: after a short turn, `maybe_compact()` is `False`, no extra client call is made, and there's no `"compacting"` status.
  - `test_maybe_compact_over_threshold`: script a reply with `usage_chunk(9600, 10)` and `compact_at=9500`, then pre-fill 12 turns of about 400 tokens each and script `completion("FACTS: x")`. Then:
    - `maybe_compact()` is `True`;
    - the statuses end with `["compacting", "idle"]`;
    - `log.events[-1] == ("compaction", {"summary": "FACTS: x"})`;
    - `agent.last_usage is None`.
  - `test_long_conversation_stays_bounded` (phase-2 acceptance):
    - Use a responder that answers streaming requests with a 600-char reply (with no usage chunk, so the estimator is exercised) and non-streaming requests with a 300-char summary.
    - Run 60 turns of 600-char user text with `compact_at=9500`, calling `maybe_compact()` after each one.
    - After each turn, `estimate_tokens(agent.messages) + estimate_tokens(agent.tools) < 9500 + 1000`.
    - `messages[0]` is unchanged.
    - No two consecutive messages are both `user`.
    - There was at least 1 compaction.
  - `test_first_status_is_still_thinking`: replaces the deleted test; on a normal turn, `ui.events[0] == ("status", "thinking")`.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_agent -v`. Expected: the new tests fail.

- [ ] **Step 3: Implement.**
  - **`maybe_compact`:** if `context_tokens() < compact_at`, return `False`. Otherwise:
    1. call `status.announce("compacting")` and print `[compacting]`;
    2. call `compact(self.client, self.model, self.messages, self.tail_budget, self.summary_max)`;
    3. if it returned a summary, call `log.compaction(summary)`;
    4. set `last_usage = None`;
    5. call `status.publish(force=True)` in a `finally`;
    6. return `True`.

    There's no separate prune step: `prune_old_tool_outputs` already runs at the end of every turn.
  - **`run_turn`:** remove the `compact_history` call.
  - **`main.py`:** pass `compact_at=int(os.getenv("COMPACT_AT", "9500"))`, `tail_budget=int(os.getenv("TAIL_BUDGET", "2500"))` and `summary_max=int(os.getenv("SUMMARY_MAX", "700"))`. Call `agent.maybe_compact()` after the turn's `try/finally`, once `ear.resume()` has run. That way speech arriving during compaction is queued, not lost.
  - **`PROTOCOL.md`:** replace "The UI also understands `compacting`, but the backend doesn't send it: trimming history is instant." with "`compacting` is sent with `StatusTracker.announce` while older turns are summarised between turns."

- [ ] **Step 4: Run** `.venv/bin/python -m unittest discover -s tests -t .` and `cd appy-ui && cargo test`. Expected: both OK.

- [ ] **Step 5: Manual check against LM Studio** (it checks the Gemma template, and that usage is reported):
  1. Run `COMPACT_AT=2500 bin/appy`.
  2. Type "my cat is called Pixel", then about 8 more messages until `[compacting]` prints.
  3. Ask "what's my cat called?".

  Expected:
  - no 400 error from the server after compaction;
  - the answer says Pixel;
  - the "compacting…" pill appeared;
  - the newest file in `~/.local/share/appy/logs/` has a `compaction` line and, after quitting, `session_end` with reason `quit`.

  If the server rejects the summary user/assistant pair, stop and report back. Don't switch to a second system message.

- [ ] **Step 6: Commit.**
```bash
git add agent.py main.py PROTOCOL.md tests/fakes.py tests/test_agent.py
git commit -m "feat: compact long sessions by summary instead of dropping history"
```
