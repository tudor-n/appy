# Memory Plan 4: Consolidator — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a session's log reaches the Mac, the memory service reads it in the background. It works out which notes the conversation touches, rewrites each of those notes with the new durable facts folded in, and stores the result as a proposal in `pending/<slug>.json` for the phase 5 review card. Nothing reaches the vault without approval.

**Architecture:** There are three new service modules, all Python 3.9, inside `appy_memory/`:
- `transcript.py` turns a JSONL log into windows of plain text, about 5k tokens each.
- `llm.py` is a minimal httpx client for LM Studio's OpenAI-compatible API.
- `consolidator.py` holds the route and rewrite prompts, the checks, and a `Consolidator` with a background `Worker` thread.

The vault gains proposal and session-progress storage. The server wakes the worker on startup and on every new log upload, and it records `/activity` pings so the worker can yield. The laptop pings `/activity` when a turn starts.

**Tech Stack:** Python 3.9 (Mac) and 3.14 (laptop), FastAPI, httpx, LM Studio (`google/gemma-4-26b-a4b`, JSON-schema `response_format`), `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-26-memory-design.md`, section E and build-order phase 4. Phase 3 (PR #5) is the base. Phase 5 (the review UI and proposal decisions) is the next plan.

**Branch:** `feat/memory-consolidator`, stacked on `feat/memory-service`, in the worktree `/home/tudor/dev/appy-memory-consolidator`. The PR base is `feat/memory-service`.

## Global Constraints

- Everything in `appy_memory/` and its tests (`tests/test_memory_*.py`, `tests/test_transcript.py`, `tests/test_consolidator.py`) runs on Python 3.9: `from __future__ import annotations`, `typing.Optional` in pydantic models, and no `tests/fakes.py`.
- **The consolidator never writes `memory/`.** It only writes `pending/<slug>.json`, `state.json` and the dumps. At most one proposal per note: `{slug, title, base_hash, new_text, summary, sessions: [], sources: [[session, window]], created, updated}`. `base_hash` is the sha256 of the note as it stands in the vault when the proposal is written, or `""` for a new note.
- **Route call:** JSON via `response_format` json_schema `{"notes":[{"slug","new","title","why"}]}`, temperature 0.1, `reasoning_effort=CONSOLIDATOR_EFFORT` (default `low`), `max_tokens` 400. Inputs: the index, `core.md`, the forgotten lines and the window. An empty list is the expected common answer. At most 3 notes per window.
- **Rewrite call:** plain markdown, temperature 0.1, same effort, `max_tokens` 1200. The input is the pending version of the note if there is one, otherwise the vault version. Output is the full note, then a final `SUMMARY: …` line.
- **Code checks every rewrite:**
  - Non-core notes: line 1 is `# <title>` (and for an existing note, the same title), and line 2 is `> <gist>` (≤120 chars).
  - Every other non-blank line starts with `- `.
  - It's under the cap (core 400 tokens, note 800). Over the cap gets one retry asking for a shorter note.
  - It differs from its input (comparing whitespace-normalised text).
  - A failed check rejects the rewrite, the reason is logged, and no proposal is written.
- **Transcript:**
  - Line formats: `[HH:MM] U: …`, `[HH:MM] A: …`, `[HH:MM] A called <name>(<args>)` and `  -> <result>`. Tool args and results are cut to 200 characters.
  - Compaction summaries are kept, as `[HH:MM] (summary of earlier conversation) …`.
  - A `turn_error` turn keeps the user's line and replaces Appy's lines with `(Appy failed to answer this; it never got a reply)`.
  - Windows are cut at turn boundaries with `CONSOLIDATOR_WINDOW_TOKENS` (default 5000), estimated with `ceil(len/3.2)`.
- **Yield:** before every LLM call, wait while the last `/activity` ping is under `CONSOLIDATOR_IDLE_SECONDS` (default 120) old, checking every 10 s.
- **Progress:** `state.json["sessions"][sid] = {"windows": n, "done_windows": k, "done": bool}`, saved after each window. A restart resumes at the first window not done. A session is done once its last window is done, crashed sessions (no `session_end`) included. An LLM transport error stops the run without marking the window done; the worker retries 300 s later or on the next kick.
- **Visibility:** each session run prints one line: `[consolidator] <sid>: <n> windows, routed [...], <p> proposals, <r> rejected (<reasons>), <tokens> tokens, <s> s`. With `CONSOLIDATOR_DUMP=1`, it writes `dumps/<sid>-w<k>-route.txt` and `dumps/<sid>-w<k>-rewrite-<slug>.txt` (prompt plus raw output) under `MEMORY_DIR`. The vault's `.gitignore` gains `dumps/`, and `open()` appends any missing entries to an existing `.gitignore`.
- **Mac env additions:** `BASE_URL=http://100.103.162.57:1234/v1` (LM Studio listens only on the Tailscale IP), `MODEL=google/gemma-4-26b-a4b`, `CONSOLIDATOR_EFFORT=low`, `CONSOLIDATOR_WINDOW_TOKENS=5000`, `CONSOLIDATOR_DUMP=1`. Without `BASE_URL`, the service runs with the consolidator off and says so.

## Review Focus

1. **The model returns junk:** slugs that aren't valid, `new:false` for a note that doesn't exist, prose instead of a note, a lost or renamed title, or code fences around the note. Each must be rejected or normalised with its reason logged, never written. Task 3: `test_route_*` and `test_rewrite_rejects_*`.
2. **LM Studio is down or slow mid-run, or the service restarts.** No window is marked done without its proposals saved, and the run resumes at the same window later. Task 4: `test_llm_error_resumes_at_same_window`.
3. **The user forgets a fact while a proposal re-adds it.** Forget already discards the note's proposal, and the forgotten lines are in both prompts. Task 3: `test_prompts_carry_forgotten_lines`.
4. **Two sessions touch the same note before review.** The second builds on the first's pending text, giving one proposal with both sessions. Task 4: `test_second_session_builds_on_pending`.
5. **A huge single turn, or a log with only `session_start`.** It still gets windows (a turn over the budget is cut to it), and an empty log is marked done with no LLM calls. Task 1: `test_windows_*`, Task 4: `test_empty_session_is_done_without_llm`.

---

### Task 1: Transcript windows

**Files:** Create `appy_memory/transcript.py`. Test: `tests/test_transcript.py`.

**Interfaces (produces):**
- `load_log(path: Path) -> list[dict]`: skips lines that aren't valid JSON objects.
- `render_turns(events: list[dict]) -> list[str]`: one string per turn. A turn starts at a `user` event, and events before the first user turn are dropped, except a `compaction`, which becomes its own turn.
- `windows(turns: list[str], budget: int) -> list[str]`: greedy packing joined with `"\n"`. A single turn over the budget is cut to `int(budget * 3.2)` chars, ending in `…`.

- [ ] **Step 1: Write the failing tests:**
  - `test_render_basic_turn`: user, assistant, tool_call, tool_result events with real `ts` render as `"[00:13] U: hi\n[00:13] A: hello\n[00:13] A called get_weather({\"day\":0})\n  -> {...}"`.
  - `test_tool_text_is_cut_to_200`
  - `test_turn_error_marks_no_reply`
  - `test_compaction_kept`
  - `test_malformed_and_unknown_lines_skipped`
  - `test_windows_pack_at_turn_boundaries`
  - `test_windows_cut_oversized_turn`
  - `test_windows_empty`
- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_transcript`. Expected: `ModuleNotFoundError`.
- [ ] **Step 3: Implement.** `HH:MM` comes from `datetime.fromisoformat(ts)` (valid on 3.9 for this format).
- [ ] **Step 4: Run** the tests again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(memory): render session logs into consolidator windows`.

### Task 2: LLM client and vault storage for proposals and progress

**Files:** Create `appy_memory/llm.py`. Modify `appy_memory/vault.py`. Tests: `tests/test_consolidator.py` (class `VaultProposalTests`) and `tests/test_memory_vault.py`.

**Interfaces (produces):**
- `class LLMError(Exception)`
- `class LLM(base_url: str, model: str, api_key: str = "", effort: str = "low", timeout: float = 300, http: httpx.Client | None = None)` with `chat(messages: list[dict], *, temperature: float, max_tokens: int, schema: dict | None = None) -> tuple[str, int]`, returning (content, total_tokens). Raises `LLMError` on any transport, HTTP or shape error.
- `Vault` gains:
  - `note_text(slug) -> str | None` and `note_hash(slug) -> str` (sha256 hex, `""` if the note is missing).
  - `proposal(slug) -> dict | None`, `proposals() -> list[dict]` (sorted by `updated`), and `save_proposal(slug, title, new_text, summary, session, window) -> dict`. Saving merges `sessions` and `sources` into an existing proposal, keeps `created`, and sets `base_hash` to the current `note_hash`.
  - `session_state(sid) -> dict` and `set_session_state(sid, windows, done_windows, done)`.
  - `sessions_to_consolidate() -> list[str]`: log stems not done, sorted.
  - `forgotten_lines() -> list[str]`
- **Fixes from the demo:**
  - The gist is cut at a word boundary, and ends with `…` when cut. (A title can't regain capitals from a lowercase slug, so "Mark i suit" stays. New notes from the consolidator take the router's `title` instead.)
  - `.gitignore` gets `dumps/`, and `open()` adds any of the five entries that are missing.

- [ ] **Step 1: Write the failing tests:**
  - **LLM** (a `httpx.MockTransport` stands in for the server):
    - `test_llm_sends_json_schema_and_effort`: the request body has `response_format.type == "json_schema"`, `reasoning_effort == "low"` and `model`.
    - `test_llm_errors`: a 500 response, a connection error and a missing `choices` each raise `LLMError`.
  - **Vault:**
    - `test_save_proposal_merges_sessions`
    - `test_base_hash_tracks_current_note`
    - `test_sessions_to_consolidate_skips_done`
    - `test_gist_cut_at_word`
    - `test_open_adds_missing_gitignore_entries`
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the vault and consolidator tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(memory): LLM client, proposal and progress storage`.

### Task 3: Route and rewrite

**Files:** Create `appy_memory/consolidator.py` (prompts, `route`, `rewrite`). Test: `tests/test_consolidator.py` (class `RouteRewriteTests`).

**Interfaces (produces):**
- `route(llm: LLM, index: str, core: str, forgotten: list[str], window: str, dump: Path | None = None) -> tuple[list[dict], int]`
  - Returns routed notes `{slug, new, title, why}` plus the tokens used.
  - Drops invalid slugs. `core` is never new. `new: true` on an existing slug becomes `new: false`. `new: false` on a missing slug becomes new with the title derived from the slug.
  - Deduplicates, and caps the result at 3.
- `class Rejected(Exception)`, with `reason: str`.
- `rewrite(llm: LLM, slug: str, current: str | None, title: str, window: str, forgotten: list[str], dump: Path | None = None) -> tuple[str, str, int]`
  - Returns (new_text, summary, tokens), or raises `Rejected`.
  - `current` is `None` for a new note.
  - Strips code fences. A missing `SUMMARY:` line → the summary `"updated <slug>"`.
- The prompts live in `ROUTE_PROMPT` and `REWRITE_PROMPT` (exact text decided during Task 5's live tuning). The rules they must carry come from spec E: durable facts only (identity, standing preferences, project facts, decisions, routines); default to nothing; never re-add forgotten lines; keep existing facts unless contradicted; one fact per bullet; third person about Tudor.

- [ ] **Step 1: Write the failing tests** with `FakeLLM(responses: list[str])`, which records each call's messages and kwargs:
  - `test_route_parses_and_validates`: bad slug dropped, core never new, the new/existing fix-ups, at most 3.
  - `test_route_empty`
  - `test_route_sends_schema`
  - `test_prompts_carry_forgotten_lines`: both prompts contain each forgotten line.
  - `test_rewrite_new_note_ok`
  - `test_rewrite_strips_fences`
  - `test_rewrite_rejects_lost_title`
  - `test_rewrite_rejects_renamed_title`
  - `test_rewrite_rejects_non_bullet_lines`
  - `test_rewrite_rejects_unchanged`
  - `test_rewrite_retries_once_when_too_long`: the second call's messages include the "shorter" request. If that's still too long → `Rejected("over cap")`.
  - `test_rewrite_core_has_no_header`: core output with a `# ` line → rejected.
  - `test_dump_files_written`
- [ ] **Step 2: Run the tests.** Expected: ImportError.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests again. Expected: PASS.
- [ ] **Step 5: Commit** `feat(memory): consolidator route and rewrite with output checks`.

### Task 4: Consolidator run, worker, server wiring and activity ping

**Files:** Modify `appy_memory/consolidator.py` (`Consolidator`, `Worker`), `appy_memory/server.py`, `memory_client.py` and `main.py`. Tests: `tests/test_consolidator.py` (class `RunTests`), `tests/test_memory_server.py` and `tests/test_memory_client.py`.

**Interfaces (produces):**
- `class Consolidator(vault: Vault, llm: LLM, lock: threading.Lock, window_tokens: int = 5000, idle_seconds: float = 120, last_activity: Callable[[], float] = lambda: 0.0, dump_dir: Path | None = None, clock=time.time, sleep=time.sleep)`, with `run_pending() -> list[dict]` (one stats dict per session processed).
  - Vault reads and writes happen under `lock`, the server's lock.
  - LLM calls happen outside the lock, so live remember and forget never wait on the model.
  - The rewrite input is re-read under the lock just before saving. If the note or its proposal changed during the call, the rewrite is discarded and redone next run.
- `class Worker(consolidator, retry_seconds: float = 300)` with `start()`, `kick()` and `stop()`. It's a daemon thread that never dies on an exception; it logs and retries.
- `create_app(vault, token, worker=None)`:
  - `POST /activity` → `{"ok": true}`, recording `time.time()`.
  - `GET /proposals` → `{"items": vault.proposals()}`.
  - A new log upload calls `worker.kick()`.
  - `app.state.last_activity` is readable by the consolidator.
- `main()` builds the LLM, Consolidator and Worker from env when `BASE_URL` is set, and kicks once at startup.
- `MemoryClient.activity() -> None`: fire-and-forget POST with a 2 s timeout that never raises. `main.py` calls it in a daemon thread at each turn start.

- [ ] **Step 1: Write the failing tests:**
  - `test_small_talk_marks_done_without_proposals`
  - `test_project_update_creates_one_proposal`: base_hash, sessions, sources and summary are set, and `memory/` is unchanged.
  - `test_second_session_builds_on_pending`: the rewrite input is the pending text, and sessions holds both.
  - `test_long_session_one_proposal_over_windows`
  - `test_crashed_session_processed`
  - `test_empty_session_is_done_without_llm`
  - `test_activity_pauses_before_llm_calls`: a fake clock makes the activity look fresh, and the sleep calls continue until it's 120 s old.
  - `test_llm_error_resumes_at_same_window`
  - `test_rejected_rewrite_does_not_block_session`
  - `test_note_changed_during_call_discards_rewrite`
  - `test_stats_line_printed`
  - Server: `test_activity_and_proposals_endpoints` and `test_upload_kicks_worker`.
  - Client: `test_activity_never_raises`.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite with `.venv/bin/python -m unittest discover -s tests -t .`. Expected: OK.
- [ ] **Step 5: Commit** `feat(memory): background consolidator with activity yield`.

### Task 5: Live tuning against LM Studio, then deploy

**Files:** Create `dev/check_consolidator.py` (fixture sessions built in code, a temporary vault, and the real LLM from the laptop `.env`). Modify the prompts in `appy_memory/consolidator.py`, `appy_memory/deploy/deploy.sh` (copy the new tests too), and `README.md`.

- [ ] **Step 1: Write the check script.** It covers the spec's cases, each run against a fresh temp vault seeded with notes. For each case it prints the routed notes, the proposals (full new_text) and PASS/FAIL:

| Case | PASS when |
|---|---|
| small talk | no proposals |
| project update to an existing note | exactly one proposal, on that note, and it contains the new fact |
| restated fact | no proposal |
| contradiction ("the PSU is now a Korad, not the Riden") | the proposal has Korad and no Riden |
| long session (3+ windows on one project) | exactly one proposal |
| crashed session (no session_end) | processed |
| last night's real session 1 log (the HUD and the MRR KPI were never saved) | proposals mention the Pi Zero 2 W HUD and MRR |

- [ ] **Step 2: Run it, read every proposal, and tune the prompts** until every case passes 2 runs in a row. Record each prompt change and its result in the ledger.
- [ ] **Step 3: Commit** `feat(memory): tuned consolidator prompts and live check`.
- [ ] **Step 4: Deploy.** Add the Mac env lines (Global Constraints) to `~/appy-memory/.env`, then run `deploy.sh`. Expected: the Mac tests pass, and the service log shows the consolidator processing the two uploaded demo sessions, with proposals visible at `GET /proposals`.
- [ ] **Step 5: Push and open the PR** against `feat/memory-service`.
