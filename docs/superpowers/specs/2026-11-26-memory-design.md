# Appy Memory & Compaction — Design (lean revision)

To be saved as `docs/superpowers/specs/2026-11-26-memory-design.md` and committed once plan mode ends.
Revises Tudor's original spec ("Appy Memory & Compaction System — Implementation Spec") toward simplicity and result quality on a 26B-A4B model with 16k context.

## Context & intent

Appy (voice + text desktop assistant: python brain `main.py`/`agent.py` and Rust/GTK overlay `appy-ui/`, both on the laptop) forgets everything between runs. Within a run, `compact_history` (`agent.py:28`) just drops the oldest 30 messages. The goal is for Appy to remember durable things about Tudor, his lab and his projects across runs, and for long runs to stay coherent. It should use as little machinery as possible, and nothing it learns should be saved without Tudor's approval.

**Success:** Appy recalls stated preferences and project facts in later runs without being told again. A long session never fails because the context is full. The vault stays small, and every change can be reviewed and undone.

## Goals / non-goals / anti-goals

- Goals: raw session logs; live compaction that measures tokens; a small vault on the Mac; background learning proposed per note and approved in the UI; explicit "remember" and "forget" by voice.
- Non-goals (v1): embeddings or vector search, NAS backup, approval by voice, per-fact review, an auto-apply mode, multi-device sync, Obsidian or human readability, defences against injected web content (no web tool exists yet; revisit when one is added).
- Anti-goals: memory that bloats the system prompt or the tool list; learned facts reaching the vault without a click in the UI; anything the model writes directly to disk.

## Constraints (verified against the code)

- **Two machines.** The Mac mini runs 24/7 and hosts the LLM (`google/gemma-4-26b-a4b` in LM Studio), the vault and the consolidator. The laptop runs Appy (`main.py` and the overlay) and connects over the network. If the Mac is unreachable, Appy can't think either, so putting the vault there adds no new way to fail.
- The model's 16k context is shared by live chat and the consolidator.
- **Session = one run of the app on the laptop** (`main()` starts, then you quit).
- The system prompt must stay byte-for-byte the same within a run, to keep the server's KV cache valid. `Agent.preheat` (`agent.py:100`) sends `messages[0]`.
- Gemma's chat template rejects two user messages in a row (see the rollback in `Agent.run_turn`).
- Every tool schema is sent with every request, so keep memory tools to 3 (4 tools in total with `get_weather`).
- Existing hooks: `Agent.run_turn` / `_run_rounds` / `_stream_round` (`agent.py`), `prune_old_tool_outputs`, `compact_history`, `StatusTracker.announce` (`status.py`), `UiBridge` (`ui_bridge.py`, FastAPI/websockets), `TOOL_SCHEMAS`/`run_tool` (`tools.py`), Rust `BackendEvent`/`ClientEvent` (`appy-ui/src/protocol.rs`), the drift test `tests/test_protocol.py`, and `PROTOCOL.md`. The UI already renders `status: compacting`.

## Approaches considered

**Consolidator** (Tudor picked option 1):
1. **Whole-note rewrite (chosen).** Route a session to the notes it touches, then rewrite each note (≤800 tokens) with the new information folded in. A review card is one note diff. Plain markdown with no ids is the most reliable output for a small model, and it needs the least code. The rewrite itself handles duplicates and contradictions.
2. Fact operations (original spec): EXTRACT then MERGE into add/update/supersede/confirm operations on lines tagged with `^id`. Allows per-fact review, but is fragile for a small model. Rejected.
3. Append to an inbox: extract bullets and append them on accept. No merging, so duplicates and stale facts pile up. Rejected.

**Review:** in the overlay UI only (accept, deny or edit, with mouse and keyboard). No voice confirmation and no auto-apply.

**Vault location** (Tudor proposed the Mac):
1. **Mac, with a small memory service (chosen).** The vault, the logs and the consolidator sit next to the model. Consolidation runs after you quit even when the laptop is asleep or off. The laptop talks to it over HTTP.
2. Laptop. One less service, but consolidation only runs while the laptop is awake, and the laptop's always-on requirement goes against how Tudor uses it. Rejected.
3. Vault on the Mac, mounted on the laptop over SMB/NFS. Git and locking over a network filesystem are flaky. Rejected.

## Design

### Storage: on the Mac, `MEMORY_DIR` (default `~/appy-data`), a local git repo
Tudor chose git for history and undo. It is local only: no remote, and separate from the appy code repo. The service runs `git commit` on every vault change (remember, forget, accepted proposal, undo). Undo is `git revert`, and commit messages carry the session id.
```
appy-data/
  memory/
    core.md          # ≤400 tokens, always injected
    <slug>.md        # flat notes; first lines: "# Title" and "> gist (≤120 chars)"
    _index.md        # generated: slug | title | gist | updated
  logs/<session_id>.jsonl
  pending/<slug>.json   # at most ONE proposal per note: {slug, base_hash, new_text, summary, sessions[], created}
  state.json         # per-session consolidation progress, forgotten-lines list, last activity time
```
- Notes are plain markdown written for the model; readability for humans and Obsidian doesn't matter. Notes are addressed by slug (`[a-z0-9-]+`), and code rejects anything else. Lines carry no metadata tags. Provenance comes from git: `git blame` points to a commit, and the commit message names the session id.
- Size caps are enforced in code: `core.md` 400 tokens, each note 800 tokens, estimated as `ceil(len/3.2)`.
- Code rebuilds `_index.md` after each commit.

### Memory service (`appy_memory/server.py`, FastAPI on the Mac, LAN or Tailscale)
It owns every read and write of the vault, and only plain code mutates files. Endpoints:
- `GET /context`: `core.md` plus the capped index, used at session start.
- `GET /note/{slug}`
- `POST /remember {text, slug?, session}`, `POST /forget {slug, text, session}`, `POST /undo`
- `POST /logs/{session_id}` (upload a finished log file, idempotent)
- `GET /proposals`, `POST /proposals/{slug} {decision, text?}`
- `POST /activity` (the laptop pings this when a turn starts, so the consolidator can yield)

Writes are serialised in-process: one lock, one writer. `MEMORY_URL` in the laptop's `.env` points at it. A shared token in `.env` is enough auth for v1.

### A. Session logs (laptop side, `appy_memory/session_log.py`)
- Written locally to `~/.local/share/appy/logs/<session_id>.jsonl`, append-only, one JSON line per event, flushed after every write. Events: `session_start{model,effort}`, `user{text,source}` (raw text, without the `[HH:MM]` prefix that `main.py` adds), `assistant{text,expression}`, `tool_call`, `tool_result` (≤4 KB), `compaction{summary}`, `turn_error{error, reply}` (the turn failed and was rolled back from the history; `reply` is the apology shown), `session_end{reason}`.
- Hooks: `main()` (start, and end in `finally`) and `Agent` (user, assistant, tools).
- When you quit, `main()` uploads the log to `POST /logs/…` in a short blocking call (a few seconds' timeout) and deletes the local copy on success. At startup it uploads any logs left behind, which covers crashes and an unreachable Mac. A log without `session_end` counts as a crashed session and is still consolidated.

### B. Live compaction (replaces `compact_history`; laptop only)
- `_stream_round` requests `stream_options={"include_usage": True}` and keeps the last `usage.prompt_tokens`. If the server doesn't report usage, fall back to an estimate of `chars/3.2` plus the schema overhead measured once at startup.
- After a turn, if `prompt_tokens ≥ COMPACT_AT` (9500), run `prune_old_tool_outputs` first. If still over:
  - Call `status.announce("compacting")`.
  - Summarise the previous summary plus the middle of the conversation with the template below (temperature 0.2, effort `none`, `max_tokens` 800).
  - Rebuild the messages as `[system, user:"[Earlier in this session — summary]…", assistant:"Got it.", …tail]`. The tail is about 2,500 tokens, starts at a user message, and never separates an assistant's tool calls from their results.
  - Log a `compaction` event.
  - Check that the user/assistant summary pair is accepted by Gemma's template.
- Update `PROTOCOL.md`, since the backend now sends `compacting`.
- Summary template (given to the model as written, from Tudor's spec):
```
Update the session summary. Keep it under 250 words. Use exactly these sections; omit empty ones.
GOAL: what the user is trying to do right now
DECISIONS: choices already made
FACTS: things the user stated that matter later (names, numbers, preferences)
OPEN: unresolved questions / pending tasks
PROMISES: anything Appy said it would do
STATE: where we left off
Previous summary:
<previous summary or "none">
New conversation to fold in:
<middle turns, tool outputs truncated to 1–2 lines>
```

### C. Session start
- The laptop calls `GET /context`. The system prompt is `prompt.txt` + `\n\n[Long-term memory]\n<core.md>\n\n[Notes you can read]\n<index, capped at ~600 tokens, most recently updated first>`.
- It is built once, before `Agent` is constructed, so `preheat` caches it, and it never changes during a run. Writes made during the run take effect in the next session.
- If the service is unreachable, start with `prompt.txt` only and log a warning.

### D. LLM tools (follow the existing pattern in `tools.py`; each one is a thin HTTP call)
- `memory_read(slug)` returns the note's text.
- `memory_remember(text, slug?)` appends a bullet to a note (or to `core.md`, or to a new note) and commits right away. The user asked explicitly, so no approval is needed.
- `memory_forget(slug, text)` removes the matching line and commits right away.
- Prompt line: "Use memory_read before answering about a person/project in the index. Only write memory when the user explicitly asks."
- There is no search tool (the index is already in context) and no approval tool.
- *Builder call:* each remember or forget sends `memory_committed {summary, kind}` to the UI, shown as a small chat chip "Remembered: … · Undo". Appy's spoken reply also confirms it, based on the tool result.
- *Builder call (undo):* undo reverts the most recent memory commit that hasn't been reverted yet: a remember, a forget, or an approved proposal. Pressing it again goes further back. There's no redo; Tudor can re-accept a proposal or say the fact again.
- *Builder call (what forget means):* forget removes the fact from the vault, but not from the logs or git history. To stop it coming back, forget also discards that note's pending proposal. The last ~20 forgotten lines are kept in `state.json` and passed to the consolidator's prompts as "do not re-add".

### E. Consolidator (on the Mac, a background task inside the memory service)
- **Trigger:** a log upload, plus a check when the service starts. It processes sessions that aren't marked `done`, oldest first.
- **Yielding to live chat:** before each LLM call it checks whether `/activity` was pinged in the last 2 minutes. If so, it waits. Progress is saved per window, so a restart picks up where it stopped.
- **Input per session:** a pruned transcript (`[HH:MM] U:` / `A:` lines, tool results cut to 200 characters, compaction summaries kept), split at turn boundaries into windows of about 5k tokens.
- **For each window:**
  1. **Route.** JSON output via `response_format` with a JSON schema; temperature 0.1, effort low, `max_tokens` 400.
     - Input: the index, `core.md`, the forgotten-lines list and the window.
     - Output: `{"notes":[{"slug","new":bool,"title?","why"}]}`.
     - An empty list is the expected common answer. The prompt says to default to nothing and keep only durable facts: identity, standing preferences, project facts, decisions and routines.
  2. **Rewrite** each routed note. Plain markdown output; temperature 0.1, effort low, `max_tokens` 1200.
     - Input: the current note, or its pending version if one exists, so several windows and sessions build up a single proposal. Also the window and the forgotten-lines list.
     - Output: the full new note, plus one `SUMMARY:` line saying what changed.
     - Code checks that the title and gist lines are intact, that the note is under the cap (otherwise it retries once, asking for a shorter note), and that something actually changed.
     - It then writes or overwrites `pending/<slug>.json`, with `base_hash` set to the hash of the note as it is in the vault now.
- `core.md` can be routed like any other note, under its strict 400-token cap.
- Each run logs: windows, routed notes, proposals, rejections with their reasons, tokens used and wall time.

### F. UI review
- When the laptop backend starts (and when it gets a proposals change), it fetches `GET /proposals` and sends them to the overlay.
- Backend → UI: `memory_proposals {items:[{slug, title, summary, before, after}]}`, sent on connect and after every change, plus `memory_committed`.
- UI → backend: `memory_decision {slug, decision: accept|deny|edit, text?}` and `memory_undo {}`. The backend forwards these to the service. Code handles them, never the LLM.
- **Accept:** if the note's hash still equals `base_hash`, write it and commit `memory: <summary> [approved] sessions=…`. Otherwise mark it stale and re-run the rewrite against the current note.
- **Edit:** accept, using Tudor's text instead. **Deny:** delete the proposal.
- **Overlay:** a collapsed card in the chat column ("2 memory updates") that expands to show each note's diff with Accept / Deny / Edit buttons. Edit opens the full proposed note as editable text.
- Add the new events to `protocol.rs` and `PROTOCOL.md`, and extend `tests/test_protocol.py`.
- Appy doesn't announce proposals out loud; the card is enough.

### Config
- Laptop `.env`: `MEMORY_URL`, `MEMORY_TOKEN`, `CONTEXT_TOKENS=16384`, `COMPACT_AT=9500`, `TAIL_BUDGET=2500`, `SUMMARY_MAX=700`.
- Mac `.env`: `MEMORY_DIR`, `MEMORY_TOKEN`, `BASE_URL`, `MODEL`, `CONSOLIDATOR_WINDOW_TOKENS=5000`, `CONSOLIDATOR_EFFORT=low`.

## Build order (each phase can ship on its own)
1. **Session logs** written locally. Accept: every turn and tool call is in the JSONL; `kill -9` leaves a valid file.
2. **Live compaction.** Accept: a scripted 60-turn fake conversation (using `tests/fakes.py`) stays under `COMPACT_AT` plus one turn; tool-call pairs are never split; a fact from before compaction is still in the summary.
3. **Memory service:** vault, git, context/note/remember/forget/undo, log upload. Laptop side: context injection and the 3 tools. Accept: bad slugs are rejected; the index regenerates; `preheat` uses the final prompt; remember and forget commit; upload is idempotent; Appy still works with the service down.
4. **Consolidator** producing `pending/`. Accept on fixture logs:
   - small talk gives no proposals;
   - a project update gives one proposal, on the right note;
   - a restated fact changes nothing;
   - a contradiction replaces the old fact;
   - a long session spans several windows but yields one proposal;
   - a crashed session gets processed;
   - an `/activity` ping pauses the run.
5. **UI review card, chip and undo.** Accept: accept commits; a stale base re-runs the rewrite; deny removes the proposal; undo reverts.

## Left to the builder
Module and package naming, exact prompts (within the rules above), diff rendering in GTK, token-estimate tuning, test structure, how git is invoked (calling the `git` command is fine), how the service runs on the Mac (a launchd plist is fine).

## Verification
- After each phase, run `.venv/bin/python -m unittest discover -s tests -t .` and `cd appy-ui && cargo test`.
- Run the service locally against a temporary `MEMORY_DIR` for tests. Replay real logs through the consolidator against LM Studio and read the pending proposals by hand before building the UI.
- Manual end-to-end test: run `bin/appy`, tell Appy a project fact, quit, wait for the proposal on the Mac, restart, accept the card, then ask about the fact in a new run.
