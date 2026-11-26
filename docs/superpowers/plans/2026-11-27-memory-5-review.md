# Memory Plan 5: Review Card, Commit Chip and Undo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tudor reviews what the consolidator learned in the overlay, and nothing else writes it. A collapsed card ("2 memory updates") above the input expands into one diff per note, each with Accept / Deny / Edit. Every memory commit (remember, forget, an accepted proposal) shows a small chat chip with Undo.

**Architecture:**
- **Service (Mac):** `POST /proposals/{slug}` applies a decision. Accept checks `base_hash`: a match commits the proposal, a mismatch marks it stale and queues a rewrite against the current note. Edit commits Tudor's text after format checks. Deny deletes the proposal. `GET /proposals` gains `before` (the note as it stands now).
- **Laptop (Python):** a new `memory_review.py` holds a `MemoryReview`. It runs on its own thread and is the only thing that talks to the service for review. It fetches proposals at start, polls every 60 s, applies decisions and undo, and pushes `memory_proposals` / `memory_committed` to the overlay through `ui_bridge.py`.
- **Overlay (Rust/GTK):** `memory.rs` holds the tray (card plus diffs plus edit box) and the chip, and renders a line diff (LCS). `protocol.rs` gets the 4 new events.

**Tech Stack:** Python 3.9 (Mac service) / 3.14 (laptop), FastAPI, GTK4 0.9 (gtk4-rs), serde, `unittest`, `cargo test`.

**Spec:** `docs/superpowers/specs/2026-11-26-memory-design.md`: section F, the "Builder call" bullets of D (the `memory_committed` chip, undo semantics) and build-order phase 5.

**Branch:** `feat/memory-review`, stacked on `feat/memory-consolidator`, in the worktree `/home/tudor/dev/appy-memory-review`.

## Global Constraints

- **Protocol** (all in `PROTOCOL.md`, `appy-ui/src/protocol.rs`, `ui_bridge.py` and the drift test `tests/test_protocol.py`):
  - Backend → UI: `memory_proposals {items: [{slug, title, summary, before, after, stale}]}` and `memory_committed {kind, summary}`, where `kind` is one of `remember`, `forget`, `approved`, `edited`, `undo`.
  - UI → backend: `memory_decision {slug, decision, text?}`, where `decision` is one of `accept`, `deny`, `edit`, and `memory_undo {}`.
  - The bridge replays the last `memory_proposals` to clients that connect late, the way it replays `status`.
- **Only code applies decisions, never the LLM.** Appy doesn't announce proposals aloud.
- **Accept:**
  - If `note_hash(slug) == base_hash`: write `new_text`, commit with the subject `memory: <summary[:60]> [approved]` and the body `sessions=<s1,s2>`, delete the proposal, and return `{"status": "accepted", "commit", "summary"}`.
  - Otherwise: set `stale: true` on the proposal, queue a rewrite, and return `{"status": "stale"}`.
  - The stale rewrite re-renders the proposal's `sources` windows and folds them one by one into the current note, then saves a fresh proposal (not merged with the stale one). If nothing is new, it deletes the proposal.
- **Edit:** Tudor's text goes through the same format checks as a rewrite (title kept, gist, bullets, cap). It's committed regardless of `base_hash` (he saw the note), with the subject `memory: <summary[:60]> [edited]`, and the proposal is deleted. Bad text → 400, with the reason shown in the card.
- **Deny:** delete the proposal; `{"status": "denied"}`.
- **Undo:** reverts the newest memory commit that hasn't been reverted yet (phase 3 semantics). The chip says `Undid: <subject without "memory: ">` and has no button. Every other chip has an Undo button, which always undoes the newest commit (spec: "Pressing it again goes further back").
- **Chip text:** `Remembered: <line>`, `Forgot: <line>`, `Saved: <summary>` (approved), `Saved your edit: <summary>`. Lines lose their leading `- `.
- **Laptop thread rule:** `ui_bridge` callbacks only enqueue. All HTTP to the service happens on `MemoryReview`'s thread. The poll runs every 60 s and only emits when the items changed.
- **Overlay:**
  - The tray is a `GtkBox` between the chat log and the input.
  - Collapsed, it's one button, `N memory update(s)`. Expanded, it shows each item's title, summary and diff (removed lines get the `diff-del` class, added lines `diff-add`), plus Accept / Deny / Edit buttons. Edit swaps the diff for a `TextView` holding `after`, with Save / Cancel.
  - Stale items show `updating…` and no buttons.
  - The tray is hidden when there are no items, and the monochrome style follows `style.css`.
  - Buttons are focusable, so Tab and Enter work while the overlay has focus.

## Review Focus

1. **A decision for a proposal that's gone** (denied from another client, or discarded by a forget): 404, and the card refreshes instead of hanging. Task 1: `test_decision_on_missing_proposal_404`. Task 2: `test_failed_decision_refreshes`.
2. **An accept racing a live remember into the same note:** the base mismatch goes stale and the rewrite runs. The accept never overwrites the newer note. Task 1: `test_accept_stale_base_queues_rewrite`.
3. **An edit with a broken format** (no title, prose lines) or over the cap: 400 with the reason, nothing written, and the card stays in edit mode showing the error. Task 1: `test_edit_validates`. Task 2: `test_edit_error_reaches_ui`.
4. **The Mac down while reviewing:** decisions fail without freezing the UI, and the card stays until a later refresh succeeds. Task 2: `test_service_down_decision_keeps_card`.
5. **Many proposals, or a long note:** the tray scrolls (max height), and the diff shows only changed lines with one line of context. Task 4: `diff_lines_*` tests.

---

### Task 1: Service decisions and stale rewrite

**Files:**
- Modify: `appy_memory/vault.py`, `appy_memory/consolidator.py` and `appy_memory/server.py`.
- Test: `tests/test_memory_server.py` (class `DecisionTests`) and `tests/test_consolidator.py` (class `RefreshTests`).

**Interfaces (produces):**
- `vault.check_note(slug, text, current) -> str` (normalised text) raising `NoteFormatError(reason)`. This is moved out of `consolidator._check` and shared by rewrite and edit; the consolidator turns it into `Rejected`.
- `Vault.apply_note(slug, text, subject, body) -> str | None` (the commit sha): writes and commits, deletes `pending/<slug>.json` and rebuilds the index.
- `Vault.delete_proposal(slug) -> bool` and `Vault.mark_stale(slug) -> None`.
- `Vault.proposals()` items gain `before` (the current note text or `""`) and `stale` (bool).
- `Consolidator.refresh(slug) -> str`, returning `"updated"`, `"deleted"` or `"missing"`.
- `Worker.refresh(slug)`: queues the slug and kicks. The loop runs queued refreshes before `run_pending`.
- `POST /proposals/{slug} {decision, text?}` → `{"status": "accepted"|"stale"|"edited"|"denied", "commit"?, "summary"?}`. A missing proposal → 404, a bad decision → 422, a bad edit → 400.

- [ ] **Step 1: Write the failing tests:**
  - `test_accept_commits_and_deletes`: subject and body checked.
  - `test_accept_new_note`
  - `test_accept_stale_base_queues_rewrite`: a stub worker records `refresh(slug)`; the note is unchanged and `stale` is true.
  - `test_deny_deletes`
  - `test_edit_commits_text`
  - `test_edit_validates`
  - `test_decision_on_missing_proposal_404`
  - `test_proposals_include_before`
  - `test_undo_reverts_approved`
  - Refresh: `test_refresh_rewrites_sources_against_current`, `test_refresh_deletes_when_nothing_new` and `test_refresh_missing`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `.venv/bin/python -m unittest discover -s tests -t .`. Expected: OK.
- [ ] **Step 5: Commit** `feat(memory): accept, deny and edit proposals; stale ones are rewritten`.

### Task 2: Laptop review coordinator, bridge events and chips for tool commits

**Files:**
- Create: `memory_review.py`.
- Modify: `memory_client.py`, `ui_bridge.py`, `main.py` and `tests/test_protocol.py`.
- Test: `tests/test_memory_review.py` and `tests/test_ui_bridge.py`.

**Interfaces (produces):**
- `MemoryClient`:
  - `proposals() -> list[dict] | None`, which maps items to `{slug, title, summary, before, after, stale}`.
  - `decide(slug, decision, text=None) -> dict`, returning the response body or `{"error": …}`.
  - `undo() -> dict`.
- `class MemoryReview(client, emit_proposals: Callable[[list], None], emit_committed: Callable[[str, str], None], poll_seconds=60)`:
  - Methods: `start()`, `stop()`, `refresh()`, `decide(slug, decision, text)`, `undo()` and `tool_committed(name, result)`.
  - The last one is called from `on_tool_result`: for `memory_remember` / `memory_forget` with a non-null `commit`, it emits a chip.
  - Everything except `tool_committed` and `start` runs on the review thread.
- `UiBridge(on_memory_decision=None, on_memory_undo=None)`:
  - `memory_proposals(items)` keeps `last_proposals` and replays it on connect.
  - `memory_committed(kind, summary)`.
  - Decision payloads are validated: `decision` must be accept, deny or edit, and an edit needs non-empty text.
- A failed decision shows its error in the card: `memory_proposals` items gain `error` for that slug until the next refresh.

- [ ] **Step 1: Write the failing tests.**
  - **MemoryReview,** with a fake client and recorded emits:
    - `test_start_emits_proposals`
    - `test_poll_only_emits_changes`
    - `test_accept_emits_chip_and_refreshes`
    - `test_stale_accept_shows_updating`
    - `test_failed_decision_refreshes`
    - `test_edit_error_reaches_ui`
    - `test_service_down_decision_keeps_card`
    - `test_undo_emits_undid_chip`
    - `test_tool_commit_chip_only_when_committed`
  - **Bridge:**
    - `test_late_client_gets_proposals`
    - `test_memory_decision_and_undo_reach_callbacks`
    - `test_bad_decisions_ignored`
  - **Drift test:** the Python kinds and decisions match the Rust enums `MemoryKind` and `Decision`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement,** then wire `main.py`:
  - Create the `MemoryReview` when memory is on, and start it after the bridge.
  - The `show_tool_card` hook also calls `review.tool_committed`.
  - Stop it in `finally`.
- [ ] **Step 4: Run** the full suite. Expected: OK.
- [ ] **Step 5: Commit** `feat(memory): review coordinator, memory events on the bridge, commit chips`.

### Task 3: Protocol types (Rust)

**Files:** Modify `appy-ui/src/protocol.rs` and `PROTOCOL.md`.

**Interfaces (produces):**
- `BackendEvent::MemoryProposals { items: Vec<Proposal> }` and `BackendEvent::MemoryCommitted { kind: MemoryKind, summary: String }`.
- `struct Proposal { slug, title, summary, before, after, #[serde(default)] stale: bool, #[serde(default)] error: Option<String> }`.
- `enum MemoryKind { Remember, Forget, Approved, Edited, Undo }`. Unknown kinds use `#[serde(other)]` → `Other`.
- `ClientEvent::MemoryDecision { slug, decision: Decision, #[serde(skip_serializing_if = "Option::is_none")] text: Option<String> }` and `ClientEvent::MemoryUndo`.
- `enum Decision { Accept, Deny, Edit }`.

- [ ] **Step 1: Write the failing Rust tests:** `parses_memory_events` and `serializes_memory_client_events`, the latter giving exact JSON such as `{"type":"memory_decision","slug":"lab","decision":"accept"}` and `{"type":"memory_undo"}`.
- [ ] **Step 2: Run** `cd appy-ui && CARGO_TARGET_DIR=/home/tudor/dev/appy-boot/appy-ui/target cargo test protocol`. Expected: compile error.
- [ ] **Step 3: Implement,** and document the events in `PROTOCOL.md`.
- [ ] **Step 4: Run** `cargo test` and the Python drift test. Expected: PASS.
- [ ] **Step 5: Commit** `feat(ui): memory protocol events`.

### Task 4: Overlay tray, diff and chip

**Files:**
- Create: `appy-ui/src/memory.rs`.
- Modify: `appy-ui/src/ui.rs` (tray placement), `appy-ui/src/events.rs`, `appy-ui/src/app.rs` (send decisions and undo), `appy-ui/src/chat.rs` (`push_chip`) and `appy-ui/src/style.css`.

**Interfaces (produces):**
- `pub fn diff_lines(before: &str, after: &str) -> Vec<DiffLine>`, where `DiffLine` is `Same`, `Del` or `Add` (String). It's an LCS over lines, keeping one line of context around changes, with `Gap` for skipped runs.
- `pub fn tray_label(n: usize) -> String`, producing "1 memory update" or "N memory updates".
- `pub fn chip_text(kind, summary) -> String`
- `struct MemoryTray` with `root: GtkBox` and `set_items(&self, items: &[Proposal], on_decide: impl Fn(String, Decision, Option<String>) + Clone + 'static)`. It keeps an item in edit mode across a refresh if that item is still present.
- `ChatLog::push_chip(text, undo: Option<impl Fn() + 'static>)`

- [ ] **Step 1: Write the failing pure-function tests:**
  - `diff_lines_marks_add_and_del`
  - `diff_lines_context_and_gaps`
  - `diff_lines_new_note_all_added`
  - `tray_label_plural`
  - `chip_text_by_kind`
- [ ] **Step 2: Run** `cargo test memory`. Expected: compile error.
- [ ] **Step 3: Implement the functions,** then the widgets and wiring, and add the CSS.
- [ ] **Step 4: Run** `cargo test`. Expected: PASS.
- [ ] **Step 5: Visual check.**
  - Run the overlay against `dev/mock_backend.py`, extended with a `--memory` flag that sends two sample proposals on connect and answers decisions with chips.
  - Take a screenshot with `spectacle -b -n -r` or `grim`, and look at the collapsed card, the expanded diff, edit mode and a chip.
  - Iterate on the CSS until it reads cleanly.
- [ ] **Step 6: Commit** `feat(ui): memory review tray, diffs, edit, commit chips with undo`.

### Task 5: End to end on the real stack

- [ ] **Step 1: Deploy** to the Mac (`deploy.sh`).
- [ ] **Step 2: Run** `bin/appy` with a scratch `APPY_LOG_DIR`. The two demo proposals (the HUD and the MRR KPI) appear as a card.
  - Accept one by clicking: a synthetic pointer click can't be done reliably on Wayland, so drive it through the same WebSocket message the button sends (`memory_decision`), and screenshot the card before and after.
  - Check the commit on the Mac, the chip, and that Undo (`memory_undo`) reverts it.
  - Deny the other.
- [ ] **Step 3: Run a session that teaches a new fact** without saying "remember". Quit, wait for the consolidator, restart, and screenshot the card showing the new proposal.
- [ ] **Step 4: Push and open the PR** against `feat/memory-consolidator`.
