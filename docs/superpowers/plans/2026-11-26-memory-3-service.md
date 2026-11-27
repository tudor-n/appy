# Memory Plan 3: Memory Service — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A small FastAPI service on the Mac mini owns a git-backed memory vault. The laptop injects `core.md` and the note index into the system prompt at startup, gives the model `memory_read` / `memory_remember` / `memory_forget`, and uploads each session log on quit and at startup.

**Architecture:** A new package `appy_memory/` is the only code deployed to the Mac. It contains `gitrepo.py` (a thin wrapper around the `git` command), `vault.py` (all file rules: slugs, caps, index, remember, forget, undo, log storage) and `server.py` (HTTP, auth and one write lock). On the laptop, the flat module `memory_client.py` wraps the HTTP calls and builds the prompt block. `tools.py` gets the 3 tool schemas, and `main.py` wires it all together. The laptop's tests run the real service in-process through Starlette's `TestClient`, which is an `httpx.Client`.

**Tech Stack:** Python 3 (laptop 3.14, Mac **3.9.6**), FastAPI, uvicorn, httpx, python-dotenv, `git` CLI, `unittest`, launchd.

**Spec:** `docs/superpowers/specs/2026-11-26-memory-design.md`: Storage, Memory service, sections A (upload part), C and D, build-order phase 3. Phases 4 (consolidator) and 5 (UI review) are later plans.

**Branch:** `feat/memory-service` from `origin/main` (4a57120), worktree `/home/tudor/dev/appy-memory-service`.

## Global Constraints

- **Python 3.9 on the Mac.** Everything under `appy_memory/` and the tests `tests/test_memory_vault.py` and `tests/test_memory_server.py` must run on 3.9.6. Each module starts with `from __future__ import annotations`. Pydantic request models use `typing.Optional[...]`, never `X | None`. No `match`, and no parenthesised `with`. Those two test files must not import `tests/fakes.py`, which evaluates `str | None` at runtime.
- `appy_memory/` imports nothing from the laptop modules. Its deps are only `appy_memory/requirements.txt`: `fastapi`, `uvicorn`, `python-dotenv`, `httpx` (the last for `TestClient`).
- **Slug:** `^[a-z0-9-]{1,64}$`. The slug `core` means `memory/core.md`. Anything else (`../x`, `Core`, `a/b`, `_index`, an empty string, 65 characters) is rejected with HTTP 400.
- **Token estimate:** `ceil(len(text) / 3.2)`. Caps: `core.md` 400, each note 800, and the index in `/context` 600 tokens (most recently updated first, dropping whole lines from the end).
- **Note file format:** line 1 `# Title`, line 2 `> gist` (≤120 chars), then `- fact` bullets. `core.md` holds bullets only. Lines carry no metadata tags.
- **Vault layout:** `MEMORY_DIR` (default `~/appy-data`) contains `memory/core.md`, `memory/<slug>.md`, `memory/_index.md`, `logs/<session_id>.jsonl`, `pending/<slug>.json` and `state.json`. The vault's `.gitignore` is exactly `logs/`, `pending/`, `state.json` and `memory/_index.md`, so the generated index can never conflict in a revert. `_index.md` is rebuilt after every change, including undo.
- **Git:** every call is `git -c user.name=Appy -c user.email=appy@localhost -c commit.gpgsign=false …` (the Mac has no git identity). A commit is made only on remember, forget and undo. Subjects are `memory: remember <slug>: <text[:60]>` and `memory: forget <slug>: <line[:60]>`, with the body `session=<id>`. The first commit is `memory: init`. Undo is `git revert --no-edit <sha>`.
- **Auth:** every endpoint requires `Authorization: Bearer <MEMORY_TOKEN>`, compared with `hmac.compare_digest`, or it returns 401. The server refuses to start without `MEMORY_TOKEN`.
- **Errors:** always JSON `{"error": "<message for the model>", ...}` with a 4xx status, never a stack trace.
- **Mac env** (`~/appy-memory/.env`): `MEMORY_DIR=~/appy-data`, `MEMORY_TOKEN`, `MEMORY_HOST=0.0.0.0`, `MEMORY_PORT=8765`.
- **Laptop env:** `MEMORY_URL=http://100.103.162.57:8765` and `MEMORY_TOKEN`. Memory is off when `MEMORY_URL` is unset: no memory tools are sent and there's no memory block.
- **Laptop timeouts:** 3 s for `/context` and the tools, 5 s per log upload. No call may raise into `main()` or a turn.
- **System prompt:** `prompt.txt` + the memory block (Task 5), built once before `Agent` is constructed. It never changes during a run.
- **Out of scope:** `/proposals`, `/activity`, the consolidator, and the `memory_committed` / `memory_undo` UI events. No `PROTOCOL.md` or `protocol.rs` changes.

## Review Focus

1. **Mac unreachable at startup or mid-run.** Appy boots with `prompt.txt` only, the tools return an error the model can relay, and the log stays local and uploads at the next start. Task 5: `test_service_down_*`.
2. **Forget with paraphrased or ambiguous text.** It must never remove the wrong line: no match → 404 listing the note's lines, more than one match → 409 listing the candidates, and the title and gist lines can never be removed. Task 2: `test_forget_*`.
3. **Remember into a full note, or with multi-line or markdown text.** Over the cap it refuses with "note is full" and writes nothing. Newlines are collapsed, so a fact can't inject a `# heading`. Task 2: `test_remember_over_cap_writes_nothing`, `test_remember_text_is_one_line`.
4. **Undo over mixed history** (remember, forget, undo, remember, undo, undo). It always reverts the newest commit that hasn't been undone, never hits a conflict, and returns `undone: null` once nothing is left. Task 1: `test_undo_target_*`, Task 2: `test_undo_sequence`.
5. **The same log uploaded twice** (a crash between upload and delete, or the startup thread killed at quit). Same bytes → 200 `stored: false`, and the laptop deletes its copy. Different bytes → 409, and the laptop keeps its copy. Task 3: `test_store_log_*`, Task 5: `test_upload_*`.

---

### Task 1: Git wrapper

**Files:**
- Create: `appy_memory/__init__.py` (empty), `appy_memory/gitrepo.py`
- Test: `tests/test_memory_vault.py` (class `GitRepoTests`)

**Interfaces:**
- Produces: `class GitError(Exception)`, plus `class GitRepo(root: Path)` with:
  - `init() -> None`: idempotent; runs `git init` if `.git` is missing.
  - `commit(subject: str, body: str = "", paths: list[str] = ["."]) -> str | None`: `git add` the paths, then commit. Returns the short sha, or `None` if nothing changed.
  - `undo_target() -> tuple[str, str] | None`: the `(sha, subject)` to revert next.
  - `revert(sha: str) -> str`: the new short sha. On failure it runs `git revert --abort` and raises `GitError`.

- [ ] **Step 1: Write the failing tests** in a temp dir, using real `git`:
  - `test_init_is_idempotent`: `init()` twice; `.git` exists.
  - `test_commit_returns_none_when_nothing_changed`.
  - `test_commit_works_without_global_identity`: run with env `GIT_CONFIG_GLOBAL=os.devnull` and `GIT_CONFIG_NOSYSTEM=1` (patched `os.environ`); the commit succeeds and the author is `Appy <appy@localhost>`.
  - `test_undo_target_skips_init_and_reverted`: commits `memory: init`, `memory: remember core: a` (c1) and `memory: remember core: b` (c2). Target is c2; after `revert(c2)` it's c1; after `revert(c1)` it's `None`.
  - `test_undo_target_after_new_commit`: c1, revert(c1), c3 → c3, then `None`. The reverted c1 is never picked again.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_memory_vault -v`. Expected: `ModuleNotFoundError: appy_memory`.

- [ ] **Step 3: Implement `appy_memory/gitrepo.py`.** Use `subprocess.run([...], cwd=root, capture_output=True, text=True)` with the `-c` flags from Global Constraints, and raise `GitError(stderr)` on a non-zero exit. `undo_target`: read `git log --format=%h%x1f%H%x1f%s%x1f%b%x1e` newest first, keeping a set `reverted`. A commit whose body has `This reverts commit <full sha>` adds that sha to `reverted` and is itself skipped. Otherwise the first commit whose subject starts with `memory: `, isn't `memory: init` and isn't in `reverted` is the target.

- [ ] **Step 4: Run the tests again.** Expected: PASS.

- [ ] **Step 5: Commit** `feat(memory): git wrapper with undo target selection`.

---

### Task 2: Vault (notes, index, context, remember, forget, undo)

**Files:**
- Create: `appy_memory/vault.py`
- Test: `tests/test_memory_vault.py` (class `VaultTests`)

**Interfaces:**
- Consumes: `GitRepo` and `GitError` (Task 1).
- Produces:
  - `class VaultError(Exception)` with `status: int`, `message: str` and `extra: dict` (e.g. `{"lines": [...]}`).
  - `estimate_tokens(text: str) -> int`
  - `CORE_CAP = 400`, `NOTE_CAP = 800`, `INDEX_CAP = 600`, `MAX_FORGOTTEN = 20`
  - `class Vault(root: Path)`:
    - `open() -> Vault`: creates the dirs, `.gitignore`, an empty `memory/core.md` and the `memory: init` commit if missing, then rebuilds the index.
    - `context() -> dict`: `{"core": str, "index": str}`
    - `read(slug: str) -> str`
    - `remember(text: str, slug: str | None, session: str) -> dict`: `{"slug", "line", "commit"}`
    - `forget(slug: str, text: str, session: str) -> dict`: `{"slug", "line", "commit"}`
    - `undo() -> dict`: `{"undone": subject | None, "commit": sha | None}`
    - `forgotten() -> list[dict]`

- [ ] **Step 1: Write the failing tests** (fresh vault in a temp dir for each):
  - `test_open_creates_layout_and_is_idempotent`: after `open()` twice, `core.md` exists, `.gitignore` lines are exactly the four entries, and there is one commit.
  - `test_bad_slugs_rejected`: for each of `"../x"`, `"Core"`, `"a/b"`, `"_index"`, `""` and `"a"*65`, `read` and `remember` raise `VaultError` with `status == 400`.
  - `test_remember_core_appends_bullet_and_commits`: `remember("Tudor lives in Sibiu.", None, "s1")` gives `core.md == "- Tudor lives in Sibiu.\n"` and `line == "- Tudor lives in Sibiu."`. The commit subject starts with `memory: remember core:` and its body contains `session=s1`.
  - `test_remember_new_note_gets_title_and_gist`: slug `lab-printer` creates a file that starts `"# Lab printer\n> Tudor's printer is a Prusa MK4.\n- Tudor's printer is a Prusa MK4.\n"`. The gist is the text cut to 120 chars.
  - `test_remember_duplicate_is_noop`: the same text twice returns `commit is None`, and the file has one bullet.
  - `test_remember_text_is_one_line`: `"  - line one\n# Evil\nline two "` is stored as `- line one # Evil line two`. Empty or whitespace-only text → 400. Text over 300 chars → 400.
  - `test_remember_over_cap_writes_nothing`: fill `core.md` close to 400 tokens, then a remember that would cross it raises 409 with "full" in the message. The file and the commit count are unchanged.
  - `test_index_regenerates`: after a remember into `lab-printer`, `_index.md` has the line `lab-printer | Lab printer | <gist> | <YYYY-MM-DD>`. After `undo()` the line is gone. `core` is never listed.
  - `test_context_index_is_capped_newest_first`: 40 notes with spaced-out mtimes (`os.utime`) give an index of ≤600 estimated tokens. The first line is the newest note.
  - `test_forget_removes_single_match`: `forget("core", "lives in sibiu", "s2")` removes the bullet case-insensitively, ignoring whitespace and trailing `.`. It returns the removed line and commits `memory: forget core: …`.
  - `test_forget_no_match_lists_lines`: → 404, `extra["lines"]` equals the note's bullets, and the file is unchanged.
  - `test_forget_ambiguous_lists_candidates`: two bullets both contain "printer" → 409, `extra["lines"]` is both bullets, and nothing is removed.
  - `test_forget_never_touches_title_or_gist`: text that matches only the `# Title` line → 404.
  - `test_forget_records_line_and_discards_pending`: create `pending/lab-printer.json` first. After the forget, the file is gone and `forgotten()[-1]` has `{"slug": "lab-printer", "line": …, "ts": …}`. After 25 forgets the list length is 20.
  - `test_undo_sequence`: remember a, forget a, undo (a is back), remember b, undo (b gone), undo (a gone again, since the remember is reverted), undo → `{"undone": None, "commit": None}`. No exception at any step.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_memory_vault -v`. Expected: the new tests fail with `ImportError` for `appy_memory.vault`.

- [ ] **Step 3: Implement `appy_memory/vault.py`.**
  - **Normalising for remember:** collapse all whitespace to single spaces, strip leading `-*•` and spaces, then strip.
  - **Normalising for forget:** also casefold, and strip trailing `.!` from both the needle and each bullet. A bullet matches when the needle is a substring of it.
  - **Title for a new note:** `slug.replace("-", " ").capitalize()`.
  - **Index `updated`:** the file's mtime date. Order by mtime descending, then by slug.
  - **Writes:** write to `<file>.tmp`, then `os.replace` (atomic) for note files and `state.json`.
  - **On error:** a failed commit or revert restores the note text that was read before the change, then raises `VaultError(500, …)`.

- [ ] **Step 4: Run the tests again.** Expected: PASS (Task 1's tests too).

- [ ] **Step 5: Commit** `feat(memory): vault with remember, forget, undo and index`.

---

### Task 3: Log storage

**Files:**
- Modify: `appy_memory/vault.py`
- Test: `tests/test_memory_vault.py` (class `LogStoreTests`)

**Interfaces:**
- Produces: `Vault.store_log(session_id: str, data: bytes) -> bool` (`True` = newly stored), and `MAX_LOG_BYTES = 20 * 1024 * 1024`.

- [ ] **Step 1: Write the failing tests:**
  - `test_store_log_new`: stored at `logs/2026-11-26_153012.jsonl` with the exact bytes; returns `True`.
  - `test_store_log_same_bytes_is_idempotent`: the second call returns `False` and the file is unchanged.
  - `test_store_log_different_bytes_conflicts`: → `VaultError` 409, and the original is kept.
  - `test_store_log_rejects_bad_input`: session ids `"../x"` and `"2026-11-26"` → 400. Non-UTF-8 bytes, a non-JSON line or a JSON array line → 400. Blank lines are allowed. Over `MAX_LOG_BYTES` → 413.
  - `test_store_log_is_not_committed`: after storing, `git status --porcelain` is empty and the commit count is unchanged.

- [ ] **Step 2: Run the tests.** Expected: FAIL (`AttributeError: store_log`).

- [ ] **Step 3: Implement** the method. Session id regex `^\d{4}-\d{2}-\d{2}_\d{6}$`, and an atomic write via a temp file plus `os.replace`.

- [ ] **Step 4: Run the tests again.** Expected: PASS.

- [ ] **Step 5: Commit** `feat(memory): idempotent session log storage`.

---

### Task 4: HTTP service

**Files:**
- Create: `appy_memory/server.py`, `appy_memory/requirements.txt`
- Test: `tests/test_memory_server.py`

**Interfaces:**
- Consumes: `Vault` and `VaultError` (Tasks 2–3).
- Produces:
  - `create_app(vault: Vault, token: str) -> FastAPI` and `main() -> None` (`python -m appy_memory.server`).
  - Endpoints:

| Endpoint | Request | Success | Errors |
|---|---|---|---|
| `GET /context` | | `{"core","index"}` | |
| `GET /note/{slug}` | | `{"slug","text"}` | 404 unknown note |
| `POST /remember` | `{"text": str, "slug": Optional[str], "session": str}` | `{"slug","line","commit"}` | |
| `POST /forget` | `{"slug": str, "text": str, "session": str}` | `{"slug","line","commit"}` | |
| `POST /undo` | `{}` | `{"undone","commit"}` | |
| `POST /logs/{session_id}` | raw request body | `{"stored": bool}` | |

  - A `VaultError` maps to its status with the body `{"error": message, **extra}`.

- [ ] **Step 1: Write the failing tests** with `TestClient(create_app(Vault(tmp).open(), "t0k"))`:
  - `test_requires_token`: no header, and a wrong token, → 401 on `/context`, `/remember` and `/logs/...`. The right token → 200.
  - `test_remember_then_read_and_context`: remember into `lab-printer`, then `GET /note/lab-printer` contains the bullet, and `/context["index"]` lists `lab-printer`.
  - `test_errors_are_json`: a bad slug → 400 `{"error": …}`. An ambiguous forget → 409 with `lines`.
  - `test_upload_is_idempotent`: POSTing the same body twice gives `stored` `true` then `false`. A different body → 409.
  - `test_undo_endpoint`: after a remember, `/undo` returns its subject. A second `/undo` → `{"undone": null, "commit": null}`.
  - `test_writes_are_serialised`: 8 threads, each with its own `TestClient` over the **same** app (a TestClient isn't thread-safe), each `POST /remember` a distinct fact to `core`. All return 200, `core.md` has 8 bullets, and there are 8 new commits.
  - `test_main_refuses_without_token`: with `MEMORY_TOKEN` unset (patched `os.environ`) and `appy_memory.server.load_dotenv` patched to a no-op (otherwise it would read a real `.env`), `main()` raises `SystemExit`.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_memory_server -v`. Expected: `ModuleNotFoundError: appy_memory.server`.

- [ ] **Step 3: Implement `appy_memory/server.py`.**
  - Use sync endpoints guarded by one `threading.Lock`; reads take it too.
  - Use auth as a dependency on every route.
  - `main()`: `load_dotenv()`, read the Mac env from Global Constraints, `Vault(Path(MEMORY_DIR).expanduser()).open()`, then `uvicorn.run`.
  - Print one line per request: `[memory] remember core "Tudor lives in Sibiu." -> 1a2b3c4 (session 2026-11-26_153012)`, likewise for forget, undo and logs (with the byte count and `new`/`same`), and errors with their status. Use `flush=True`, because launchd captures stdout.

- [ ] **Step 4: Run the tests again.** Expected: PASS.

- [ ] **Step 5: Run the full suite** with `.venv/bin/python -m unittest discover -s tests -t .`. Expected: all pass.

- [ ] **Step 6: Commit** `feat(memory): FastAPI memory service`.

---

### Task 5: Laptop client and prompt block

**Files:**
- Create: `memory_client.py`
- Test: `tests/test_memory_client.py` (laptop-only, so `str | None` is fine here)

**Interfaces:**
- Consumes: `create_app` and `Vault` for in-process tests only.
- Produces:
  - `class MemoryClient(http: httpx.Client, session_id: str)` with:
    - `from_env(session_id: str) -> MemoryClient | None` (classmethod): `None` when `MEMORY_URL` is unset. Otherwise `httpx.Client(base_url=MEMORY_URL, headers={"Authorization": f"Bearer {MEMORY_TOKEN}"}, timeout=3.0)`.
    - `context() -> dict | None`: `None` on any failure, after printing `[memory] unreachable (<ExcName>): starting without long-term memory`.
    - `read(slug: str) -> str`, `remember(text: str, slug: str | None = None) -> str` and `forget(slug: str, text: str) -> str`. Each returns a JSON string for the model: the service's body on 2xx or 4xx, otherwise `{"error": "long-term memory is unreachable right now"}`.
    - `upload_log(path: Path) -> bool`: POSTs the file (timeout 5 s) and deletes it on 2xx. A missing file → `False`.
    - `upload_leftovers(logs_dir: Path, skip: Path) -> int`: uploads every `*.jsonl` except `skip`, oldest name first, and returns the count uploaded.
  - `MEMORY_RULES = "Use memory_read before answering about a person/project in the index. Only write memory when the user explicitly asks."`
  - `system_prompt(base: str, context: dict | None) -> str`: `base` unchanged when `context` is `None`. Otherwise it returns exactly:
    `f"{base}\n\n[Long-term memory]\n{core or '(empty)'}\n\n[Notes you can read]\n{index or '(none yet)'}\n\n{MEMORY_RULES}"`, with `core` and `index` stripped.

- [ ] **Step 1: Write the failing tests.** The `http` is `TestClient(create_app(vault, "t"), headers={"Authorization": "Bearer t"})`, which is an `httpx.Client`:
  - `test_remember_read_forget_roundtrip`: the session id reaches the commit body (`session=2026-11-26_153012`).
  - `test_service_errors_reach_the_model`: `read("Bad")` → JSON with `error`.
  - `test_service_down_context_is_none`: `httpx.Client(base_url="http://127.0.0.1:9", timeout=0.5)` gives `context() is None`.
  - `test_service_down_tools_return_error_json`: all three tools return the unreachable error, and none raises.
  - `test_service_down_upload_keeps_file`: returns `False` and the file still exists.
  - `test_upload_deletes_on_success_and_is_idempotent`: upload, recreate the same file, upload again. Both return `True` and the file is gone.
  - `test_upload_conflict_keeps_file`: → `False` and the file is kept.
  - `test_upload_leftovers_skips_current`: three files in the dir with `skip` set to one of them uploads two, and the skipped file remains. `.txt` files are ignored.
  - `test_system_prompt_block`: the exact string above for `{"core": "- a", "index": ""}`, and `system_prompt("P", None) == "P"`.
  - `test_from_env_off_without_url`: `MEMORY_URL` unset → `None`.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_memory_client -v`. Expected: `ModuleNotFoundError: memory_client`.

- [ ] **Step 3: Implement `memory_client.py`.** Catch `httpx.HTTPError` and `ValueError` (bad JSON) around every call.

- [ ] **Step 4: Run the tests again.** Expected: PASS.

- [ ] **Step 5: Commit** `feat(memory): laptop memory client and prompt block`.

---

### Task 6: Tools and main.py wiring

**Files:**
- Modify: `tools.py` (schemas, functions, `TOOL_MAP`), `main.py`
- Test: `tests/test_memory_tools.py`

**Interfaces:**
- Consumes: `MemoryClient` and `system_prompt` (Task 5).
- Produces:
  - `tools.MEMORY_TOOL_SCHEMAS: list[dict]` (3 entries)
  - `tools.set_memory(client: MemoryClient | None) -> None`
  - `tools.memory_read(slug)`, `tools.memory_remember(text, slug=None)` and `tools.memory_forget(slug, text)`, all registered in `TOOL_MAP`. With no client they return `{"error": "long-term memory isn't set up"}`.
  - Schemas, same shape as `get_weather`:

| Tool | Description | Parameters |
|---|---|---|
| `memory_read` | "Read one long-term memory note. slug must be one listed under [Notes you can read]." | `slug` (string), required |
| `memory_remember` | "Save a fact to long-term memory. Only call this when the user explicitly asks you to remember something. Leave out slug for facts about the user themselves (core memory); give a slug to file it under a note (a new slug creates the note)." | `text`: "One short, self-contained sentence, e.g. 'Tudor's lab printer is a Prusa MK4.'", required. `slug`: "Note slug: lowercase letters, digits and dashes." |
| `memory_forget` | "Remove a fact from long-term memory when the user asks you to forget it." | `slug`: "'core' or a note slug". `text`: "The fact to remove, as it appears in memory." Both required. |

- [ ] **Step 1: Write the failing tests:**
  - `test_four_tools_with_memory`: the names in `TOOL_SCHEMAS + MEMORY_TOOL_SCHEMAS` are exactly `get_weather, memory_read, memory_remember, memory_forget`.
  - `test_run_tool_routes_to_memory`: a stub client records calls; `run_tool("memory_remember", '{"text":"x"}')` calls `remember("x", None)`.
  - `test_memory_tools_without_client`: after `set_memory(None)`, each tool returns the "isn't set up" error.
  - `test_memory_tools_are_not_stale`: none is in `STALE_TOOLS`.
  - `test_preheat_uses_final_prompt`: build `system_prompt("BASE", {"core": "- a", "index": "x | X | g | 2026-11-26"})`, construct `Agent(FakeClient([completion("")]), …, system_prompt=that, tools=TOOL_SCHEMAS + MEMORY_TOOL_SCHEMAS)` and call `preheat()`. `calls[0]["messages"][0]["content"]` is that exact string and the request has 4 tools.

- [ ] **Step 2: Run** `.venv/bin/python -m unittest tests.test_memory_tools -v`. Expected: `AttributeError: MEMORY_TOOL_SCHEMAS`.

- [ ] **Step 3: Implement the `tools.py` part.**

- [ ] **Step 4: Wire `main.py`**, right after `log = SessionLogger(...)`:
  - `memory = MemoryClient.from_env(log.session_id)`, then `tools.set_memory(memory)`.
  - `context = memory.context() if memory else None`. Print one of:
    - `[memory] core ~N tokens, M notes`
    - the unreachable line from Task 5
    - `[memory] off (MEMORY_URL not set)`
  - Pass `Agent(system_prompt=system_prompt(load_system_prompt(), context), tools=tools.TOOL_SCHEMAS + (tools.MEMORY_TOOL_SCHEMAS if memory else []), …)`.
  - When `APPY_DUMP_PROMPT=1`, write the final prompt to `log.path.with_suffix(".prompt.txt")`.
  - If `memory` is set, start `upload_leftovers(log.path.parent, log.path)` in a daemon thread.
  - In `finally`, after `log.end(...)`: `if memory: memory.upload_log(log.path)`, then print `[memory] log uploaded` or `[memory] log kept locally`.

- [ ] **Step 5: Run the full suite** with `.venv/bin/python -m unittest discover -s tests -t .`. Expected: all pass.

- [ ] **Step 6: Smoke test with the service down.** Run `cd /home/tudor/dev/appy-memory-service && MEMORY_URL=http://127.0.0.1:9 MEMORY_TOKEN=x APPY_DUMP_PROMPT=1 bin/appy` in the background.
  - Expected: `[memory] unreachable …`, then `Appy ready` within about 3 s of the usual boot.
  - The `.prompt.txt` holds just `prompt.txt`.
  - Stop it with `kill -INT <pid>`. Expected: `[memory] log kept locally`.

- [ ] **Step 7: Commit** `feat(memory): memory tools, context injection and log upload`.

---

### Task 7: Live check of the tools against LM Studio

**Files:**
- Create: `dev/check_memory_tools.py`

- [ ] **Step 1: Write the script.**
  - It loads `.env` and builds `system_prompt(prompt.txt, sample)`, where `sample` is core `- Tudor lives in Sibiu.` plus an index with `appy | Appy | Tudor's voice assistant project | 2026-11-26` and `lab-printer | Lab printer | Prusa MK4 setup | 2026-11-20`.
  - It sends each prompt below 3 times with the 4 schemas, `reasoning_effort="none"` and `temperature=0.4`, non-streamed, prefixed `[09:00 PM]` the way `main.py` does.
  - It prints each run's tool calls and arguments, then a tally.

| Prompt | Expected |
|---|---|
| "remember that my bench PSU is a Riden RD6024" | `memory_remember` |
| "what do you know about the appy project?" | `memory_read` with slug `appy` |
| "forget that I live in Sibiu" | `memory_forget` with slug `core` |
| "what's the weather?" | `get_weather` |
| "I bought a new keyboard today" | no `memory_*` call |

- [ ] **Step 2: Run** `.venv/bin/python dev/check_memory_tools.py`.
  - **Pass bar:** each expected call happens in ≥2/3 runs, and there are 0/3 memory writes on the casual line.
  - If it falls short, stop and show Tudor the tally before changing anything. A `forced_call` rule for "remember/forget that…" is the likely fix, but it is his call.

- [ ] **Step 3: Commit** `dev: live check for memory tool calling`, with the tally in the commit body.

---

### Task 8: Deploy to the Mac mini and end-to-end check

**Files:**
- Create: `appy_memory/deploy/local.appy.memory.plist`, `appy_memory/deploy/deploy.sh`
- Modify: `README.md` (a new "Long-term memory" section after "Session logs and compaction"), `.env.example` (add `MEMORY_URL=` and `MEMORY_TOKEN=`, and fix the missing newline in `PROMPT_FILE=prompt.txtREASONING_EFFORT=none`)

- [ ] **Step 1: Write the plist.**
  - Label `local.appy.memory`.
  - `ProgramArguments`: `/Users/tudor/appy-memory/.venv/bin/python -m appy_memory.server`.
  - `WorkingDirectory`: `/Users/tudor/appy-memory`.
  - `RunAtLoad` and `KeepAlive` both true.
  - `EnvironmentVariables` `PATH=/opt/homebrew/bin:/usr/bin:/bin`. launchd's default PATH lacks Homebrew, and git lives there.
  - stdout and stderr go to `/Users/tudor/appy-memory/service.log`.

- [ ] **Step 2: Write `deploy.sh`**, run from the repo root on the laptop with `set -euo pipefail`. In order:
  1. `rsync -a --delete appy_memory/ macmini:appy-memory/appy_memory/`, and rsync `tests/__init__.py`, `tests/test_memory_vault.py` and `tests/test_memory_server.py` to `macmini:appy-memory/tests/`.
  2. On the Mac:
     - Stop with instructions if `~/appy-memory/.env` is missing.
     - Create `.venv` with `/usr/bin/python3 -m venv` if it's absent, then `pip install -q -r appy_memory/requirements.txt`.
     - Run `.venv/bin/python -m unittest tests.test_memory_vault tests.test_memory_server`. This is the Python 3.9 check.
     - Copy the plist to `~/Library/LaunchAgents/`.
     - Run `launchctl bootout gui/$(id -u)/local.appy.memory || true`, then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.appy.memory.plist`.
  3. On the laptop, wait up to 10 s for `curl -fsS -H "Authorization: Bearer $MEMORY_TOKEN" "$MEMORY_URL/context"` to succeed, with both values taken from `.env`.

- [ ] **Step 3: Write the README section**: what lives on the Mac, the env vars on both sides, `deploy.sh`, `APPY_DUMP_PROMPT=1`, where `service.log` is, and that undo is `curl -X POST …/undo` until phase 5 adds the button.

- [ ] **Step 4: Commit** `feat(memory): launchd deploy for the Mac mini`.

- [ ] **Step 5: Ask Tudor before touching the Mac.** Get his OK to create `~/appy-memory/.env` (with the token) and install the LaunchAgent. Then run `appy_memory/deploy/deploy.sh`. Expected: the Mac's unit tests pass on 3.9.6 and the `curl` returns `{"core":"","index":""}`.

- [ ] **Step 6: End-to-end test.** Tudor adds `MEMORY_URL` and `MEMORY_TOKEN` to the laptop `.env`. Run `APPY_DUMP_PROMPT=1 bin/appy`.
  1. Say "remember that my bench PSU is a Riden RD6024". `ssh macmini git -C ~/appy-data log -1` shows the commit with `session=<id>`.
  2. Quit with `kill -INT`. The log shows up in `~/appy-data/logs/` and is gone from `~/.local/share/appy/logs/`.
  3. Restart. The `.prompt.txt` dump contains the fact under `[Long-term memory]`, and asking "what PSU do I have?" answers from it.
  4. "Forget that my bench PSU is a Riden" removes it. `curl -X POST …/undo` restores it.

- [ ] **Step 7: Run both suites**: `.venv/bin/python -m unittest discover -s tests -t .` and `cd appy-ui && CARGO_TARGET_DIR=/home/tudor/dev/appy-boot/appy-ui/target cargo test`. Then push `feat/memory-service` and open a PR against `main`; Tudor merges.
