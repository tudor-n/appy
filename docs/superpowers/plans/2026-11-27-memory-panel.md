# Memory Review Panel, Live Updates and Auto-Approve — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move memory review into its own floating, paged panel above the chat (with Approve all / Later), tell Appy about memory changes on his next turn, and add an auto-approve toggle to the panel pill's menu.

**Architecture:** The Mac service gains a `settings.json` (auto-approve), an auto-commit hook in the consolidator, `GET/PUT /settings` and a `GET /changes` feed of `[auto]` commits. The brain's `MemoryReview` relays settings and auto commits to the overlay and hands one line per memory change to the `Agent`, which prepends them to the next user message. The overlay replaces the in-chat `MemoryTray` with a `MemoryPanel` overlay child positioned by a pure `layout::memory_slot` (above the chat, flipping below), paging with a `gtk4::Stack`.

**Tech Stack:** Python 3.14 laptop / Python 3.9 Mac (FastAPI, httpx, pytest), Rust gtk4-rs 0.9 + gtk4-layer-shell, Plasma 6 QML pill.

**Spec:** `docs/superpowers/specs/2026-11-27-memory-panel-design.md` (builds on `docs/superpowers/specs/2026-11-26-memory-design.md`).

## Global Constraints

- Mac code (`appy_memory/`, its tests) stays Python 3.9-compatible: `from __future__ import annotations`, `Optional[...]` in pydantic models, Mac tests must not import `tests/fakes.py`.
- Every vault git call goes through `GitRepo._git` (it passes `-c user.name=Appy -c user.email=appy@localhost`).
- Commit subjects: `memory: <summary[:60]> [auto]` for auto commits (beside the existing `[approved]` / `[edited]`).
- `settings.json` is gitignored in the vault; default `{"auto_approve": false}`.
- No "deny all". Later sends nothing to the brain or the Mac.
- The system prompt is never rebuilt mid-session.
- Laptop tests: `.venv/bin/python -m pytest -q`; overlay: `cargo test --manifest-path appy-ui/Cargo.toml`; Mac: run by `appy_memory/deploy/deploy.sh`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never merge PRs. Don't touch `/home/tudor/dev/appy`.

## Review Focus

1. Auto-approve turned on while a proposal is stale or its note moved: it must be redone, then auto-committed — never committed over a newer note. (Task 1 test `test_auto_approve_on_redoes_a_moved_note`.)
2. A turn that fails after memory notes were prepended: the notes must survive for the next turn, not be lost with the rolled-back message. (Task 2 test `test_memory_notes_survive_a_failed_turn`.)
3. Later pressed, then the same list arrives again on the 60 s poll: the panel must stay hidden; a genuinely new or changed proposal brings it back. (Task 4 test `later_hides_until_something_new`.)
4. Appy dragged across the top edge with the chat open: the panel flips below without flickering at the boundary. (Task 4 test `memory_slot_hysteresis`.)
5. Moving the pointer from the chat to the panel (crossing the click-through gap) or typing in Edit must not close the chat and hide the panel. (Task 5: hover/focus wiring + live check.)

---

### Task 1: Mac service — settings, auto-commit, changes feed

**Files:**
- Modify: `appy_memory/gitrepo.py`, `appy_memory/vault.py`, `appy_memory/consolidator.py`, `appy_memory/server.py`
- Test: `tests/test_memory_vault.py`, `tests/test_memory_server.py`, `tests/test_consolidator.py`

**Interfaces:**
- Produces:
  - `GitRepo.head() -> str` (`""` in an empty repo); `GitRepo.log_after(sha: str) -> list[tuple[str, str]]` — `(sha, subject)` oldest first for `sha..HEAD`; `[]` for an empty or unknown sha.
  - `Vault.settings() -> dict` (`{"auto_approve": bool}`), `Vault.set_settings(auto_approve: bool) -> dict`; `GITIGNORE` gains `settings.json`.
  - `Vault.auto_commit(slug: str) -> str | None` — if auto-approve is on and the proposal exists, isn't stale and `base_hash == note_hash(slug)`: `apply_note(slug, new_text, f"memory: {summary[:60]} [auto]", "sessions=...")`, return the sha; else `None`.
  - `Vault.changes(after: str) -> dict` — `{"head": str, "items": [{"commit", "slug", "summary"}]}`, only `[auto]` commits, `summary` without `memory: ` and ` [auto]`, `slug` from the commit's one `memory/<slug>.md` file.
  - HTTP: `GET /settings`; `PUT /settings` body `{"auto_approve": bool}` → `{"auto_approve", "accepted": [slug], "stale": [slug]}` (turning on accepts every waiting proposal as `[auto]`, moved ones go stale + refresh); `GET /changes?after=<sha>`.
  - Undo of an `[auto]` commit is a plain revert (no proposal restored).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_memory_vault.py
def test_settings_default_and_persist(vault):
    assert vault.settings() == {"auto_approve": False}
    vault.set_settings(auto_approve=True)
    assert Vault(vault.root).open().settings() == {"auto_approve": True}
    assert "settings.json" in (vault.root / ".gitignore").read_text()

def test_auto_commit_only_when_on_and_base_current(vault):
    vault.save_proposal("bench", "Bench", "# Bench\n> gear\n- PSU is a Korad.\n", "PSU is a Korad", "s1", 0)
    assert vault.auto_commit("bench") is None            # off: stays pending
    vault.set_settings(auto_approve=True)
    head = vault.git.head()
    sha = vault.auto_commit("bench")
    assert sha and vault.proposal("bench") is None
    assert vault.git.log_after("")  == []                  # empty sha -> nothing
    assert vault.changes("")["items"] == []
    ch = vault.changes(head)
    assert ch["items"] == [{"commit": sha, "slug": "bench", "summary": "PSU is a Korad"}]

def test_auto_commit_skips_a_moved_note(vault): # base_hash != note_hash -> None, proposal kept
def test_undo_of_auto_commit_does_not_restore_a_proposal(vault):
    # auto commit, undo -> note reverted, vault.proposal(slug) is None
def test_changes_ignores_other_commits_and_unknown_sha(vault):
    # remember + [approved] commits are not in items; changes("deadbeef") -> items []
```

```python
# tests/test_memory_server.py
def test_settings_endpoints(client): GET -> {"auto_approve": False}; PUT true -> 200, GET true; PUT {"auto_approve": "x"} -> 400
def test_auto_approve_on_accepts_waiting_proposals(client, vault):
    # two waiting proposals -> PUT true -> "accepted" has both, both notes committed with " [auto]" subjects
def test_auto_approve_on_redoes_a_moved_note(client, vault, fake_worker):
    # proposal built on an old note, note changed by /remember -> PUT true -> "stale": [slug],
    # proposal marked stale, worker.refresh(slug) called, note NOT overwritten
def test_changes_endpoint(client): GET /changes?after= -> {"head": <sha>, "items": []}
```

```python
# tests/test_consolidator.py
def test_auto_approve_commits_new_proposals(...):  # settings on -> run_pending leaves no pending/, note updated, subject ends " [auto]"
def test_auto_approve_commits_a_refreshed_proposal(...):  # stale proposal + refresh() with auto on -> committed
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_memory_vault.py tests/test_memory_server.py tests/test_consolidator.py`
Expected: the new tests FAIL (`AttributeError: 'Vault' object has no attribute 'settings'`, 404/405 for the endpoints).

- [ ] **Step 3: Implement**

`settings.json` via the existing `_write_atomic`. In `Consolidator._note` call `self.vault.auto_commit(slug)` right after `save_proposal` (same lock block); in `refresh` right after `replace_proposal`. Print `[consolidator] auto-saved {slug}` when it commits. In `server.py` move the accept branch of `decide` into a local `accept(slug, item, tag) -> dict` used by `decide` (`tag="approved"`) and `PUT /settings` (`tag="auto"`), all under `lock`. `SettingsBody(BaseModel): auto_approve: bool` (strict bool: use `StrictBool`).

- [ ] **Step 4: Run to verify they pass, then the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

`git commit -m "feat(memory): auto-approve setting, [auto] commits and a changes feed"`

---

### Task 2: Brain — client, review relay, live memory notes

**Files:**
- Modify: `memory_client.py`, `memory_review.py`, `ui_bridge.py`, `agent.py`, `main.py`
- Test: `tests/test_memory_client.py`, `tests/test_memory_review.py`, `tests/test_ui_bridge.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: Task 1 HTTP endpoints.
- Produces:
  - `MemoryClient.settings() -> dict | None`, `MemoryClient.set_settings(auto_approve: bool) -> dict` (`{"error": ...}` on failure, like `decide`), `MemoryClient.changes(after: str) -> dict | None`.
  - `MemoryReview(client, emit_proposals, emit_committed, emit_settings=None, on_change=None, poll_seconds=POLL_SECONDS)`; `set_auto_approve(on: bool)`. On start and every poll: `changes(after=head)`; each item → `emit_committed("auto", summary)` and `on_change(f"saved to {slug}: {summary}")`. Accept → `on_change(f"saved to {slug}: {summary}")`; edit → `on_change(f"saved Tudor's edit to {slug}: {summary}")`; undo → `on_change(f"undid {subject}")`. Settings sent via `emit_settings(bool)` on start and after every toggle (also when the call failed, with the Mac's value, so the pill shows the truth).
  - Bridge: `MEMORY_KINDS` gains `"auto"`; `UIBridge.memory_settings(auto_approve: bool)` emits `{"type": "memory_settings", "auto_approve": bool}` and replays it to a reconnecting overlay (like `last_proposals`); inbound `{"type": "memory_auto_approve", "on": bool}` → `on_memory_auto_approve(on)` (non-bool `on` ignored).
  - `Agent.note_memory_change(line: str) -> None` (thread-safe). `run_turn` sends `f"[Memory updated since your last reply: {'; '.join(lines)}]\n\n{user_text}"` as the user message content when lines are waiting, then clears them; on rollback they go back in front of any newer lines.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent.py
def test_memory_notes_prefix_the_next_user_message(agent_with_fake_llm):
    agent.note_memory_change("saved to lumen-project: first KPI is MRR")
    agent.run_turn("what's my first KPI?")
    assert sent_messages[-1]["content"] == ("[Memory updated since your last reply: saved to "
        "lumen-project: first KPI is MRR]\n\nwhat's my first KPI?")
    agent.run_turn("thanks")                   # notes are used once
    assert last_user_content == "thanks"

def test_memory_notes_survive_a_failed_turn(...):   # LLM raises -> next turn still carries the note
```

```python
# tests/test_memory_review.py
def test_auto_commits_become_chips_and_notes(fake_client):  # changes feed with one item -> emit_committed("auto", ...), on_change("saved to bench: ...")
def test_head_is_taken_at_start_so_old_auto_commits_are_not_replayed(...)
def test_accept_edit_undo_reach_on_change(...)
def test_set_auto_approve_emits_the_macs_value(...)  # set_settings ok -> emit_settings(True); error -> emit_settings(<settings() value>)
```

```python
# tests/test_ui_bridge.py
def test_memory_settings_event_and_replay(...)
def test_memory_auto_approve_inbound(...)  # {"on": true} -> callback(True); {"on": "yes"} -> ignored
# tests/test_memory_client.py: settings / set_settings / changes against a MockTransport, incl. service down -> None / {"error"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_agent.py tests/test_memory_review.py tests/test_ui_bridge.py tests/test_memory_client.py`
Expected: new tests FAIL (`AttributeError`).

- [ ] **Step 3: Implement**, wiring in `main.py`: `MemoryReview(memory, ui.memory_proposals, ui.memory_committed, emit_settings=ui.memory_settings, on_change=agent.note_memory_change)` — the agent is created after the review today, so create the review after the agent (or pass a forwarding lambda through `review_box`). `on_memory_auto_approve` forwards to `review.set_auto_approve`.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -q` — Expected: all pass.

- [ ] **Step 5: Commit** `feat(memory): live memory notes for Appy, auto commits and settings relayed to the overlay`

---

### Task 3: Overlay protocol + panel pill toggle

**Files:**
- Modify: `appy-ui/src/protocol.rs`, `appy-ui/src/net/pill_bridge.rs`, `appy-ui/src/events.rs`, `appy-ui/src/app.rs`, `appy-ui/src/state.rs`, `appy-ui/src/memory.rs` (chip text), `appy-pill/contents/ui/main.qml`

**Interfaces:**
- Produces:
  - `MemoryKind::Auto` (`"auto"`), `chip_text(Auto, s) == "Auto-saved: s"`, `undo_label(Auto) == Some("Undo")`.
  - `BackendEvent::MemorySettings { auto_approve: bool }`; `ClientEvent::MemoryAutoApprove { on: bool }` → `{"type":"memory_auto_approve","on":true}`.
  - `PillSyncState.auto_approve: Option<bool>` (serialized as `null` until the brain sends settings; `SharedState.auto_approve: Option<bool>` holds it).
  - Pill route `POST /auto-approve` → `UiEvent::ToggleAutoApprove` → app sends `MemoryAutoApprove { on: !current }` (nothing when `None`).
  - QML: a checkable `PlasmaCore.Action` "Auto-approve memories", `visible: root.connected && root.autoApprove !== null`, `checked: root.autoApprove === true`, POSTs `/auto-approve`; `autoApprove` read from `/state` (`data.auto_approve`, `null` when missing).

- [ ] **Step 1: Failing tests** in `protocol.rs` (parse `memory_settings`, `memory_committed` kind `auto`, serialize `MemoryAutoApprove`, `PillSyncState::default()` serializes `"auto_approve":null`), `pill_bridge.rs` (`route("POST /auto-approve ...") == Route::AutoApprove`), `memory.rs` (chip text / undo label for `Auto`).
- [ ] **Step 2:** `cargo test --manifest-path appy-ui/Cargo.toml` → new tests FAIL to compile/assert.
- [ ] **Step 3: Implement**; `sync_pill` copies `s.auto_approve`.
- [ ] **Step 4:** `cargo test` → all pass; `qmllint`-free check: `grep -c auto-approve appy-pill/contents/ui/main.qml` ≥ 1.
- [ ] **Step 5: Commit** `feat(ui): auto-approve toggle in the panel pill's menu`

---

### Task 4: Panel state and placement (pure logic)

**Files:**
- Modify: `appy-ui/src/memory.rs` (`TrayState` → paging, Later, Approve all), `appy-ui/src/layout.rs`
- Test: unit tests in both files

**Interfaces:**
- Produces (memory.rs, on `TrayState`):
  - `page: usize` (clamped in `set_items` to `items.len() - 1`; the item at the old index after an accept is the next one), `next_page()`, `prev_page()`, `page_label(&self) -> String` (`"1 of 3"`, `""` when empty).
  - `later(&mut self)` — hides until the list holds a `(slug, after)` pair that wasn't there when Later was pressed; `is_hidden(&self) -> bool`.
  - `approve_all(&mut self) -> Vec<Outgoing>` — Accept for every item that is not stale, not busy and not being edited; marks them busy.
  - `can_approve_all(&self) -> bool`.
- Produces (layout.rs):
  - `pub enum VSide { Above (default), Below }`
  - `pub const MEMORY_GAP: f64 = 10.0;`
  - `pub fn memory_slot(chat: Rect, panel: Size, win: Size, side: HSide, current: VSide, sticky: bool) -> (Point, VSide)` — x: the panel's outer edge lines up with the chat's outer edge (`side == Right` → right edges equal; `Left` → left edges equal), then clamped into the screen. y: above = `chat.y - MEMORY_GAP - h`, below = `chat.y + chat.h + MEMORY_GAP`. Non-sticky: Above if it fits (y ≥ 0), else whichever overflows less. Sticky (dragging): Above → Below once above overflows by more than `FLIP_MARGIN` and below overflows less; Below → Above once above has `FLIP_MARGIN` to spare.
  - `card_slot` gains `avoid: Option<Rect>`: a candidate overlapping `avoid` counts as not fitting (falls back to least overflow + overlap as today).

- [ ] **Step 1: Failing tests**

```rust
#[test] fn pages_follow_the_list() // 3 items, next_page twice -> label "3 of 3"; accept item 3 -> list of 2 -> page 1, "2 of 2"; empty -> ""
#[test] fn later_hides_until_something_new() // later(); same items again -> hidden; same slug new `after` -> shown; new slug -> shown
#[test] fn approve_all_skips_stale_busy_and_editing() // items a(stale), b(editing), c(busy), d -> one Outgoing for d; can_approve_all false afterwards
#[test] fn memory_slot_prefers_above() // chat at y=500 h=300, panel 360x260, win 1920x1080 -> Above, y = 500-10-260
#[test] fn memory_slot_flips_below_near_the_top() // chat.y = 100 -> Below, y = 100+300+10
#[test] fn memory_slot_hysteresis() // sticky, current Above, above overflows by 10 (< FLIP_MARGIN) -> stays Above; by 30 -> Below; current Below, above has 10 spare -> stays Below; 20 spare -> Above
#[test] fn memory_slot_aligns_outer_edge_and_clamps() // side Right: panel right edge == chat right edge; Left: left edges equal; near x=0 clamped to 0
#[test] fn card_slot_avoids_the_memory_panel() // `away` side blocked by avoid -> picks `above`
```

- [ ] **Step 2:** `cargo test --manifest-path appy-ui/Cargo.toml` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** `cargo test` → all pass.
- [ ] **Step 5: Commit** `feat(ui): memory panel paging, Later, Approve all and placement`

---

### Task 5: The floating memory panel widget

**Files:**
- Modify: `appy-ui/src/memory.rs` (`MemoryTray` → `MemoryPanel`), `appy-ui/src/ui.rs`, `appy-ui/src/tick.rs`, `appy-ui/src/controls.rs`, `appy-ui/src/state.rs`, `appy-ui/src/app.rs`, `appy-ui/src/style.css`

**Interfaces:**
- Consumes: Task 4 state and `layout::memory_slot`, `card_slot(.., avoid)`.
- Produces: `MemoryPanel { root: GtkBox, .. }` with `new()`, `set_on_send`, `set_items`, `is_wanted(&self) -> bool` (items non-empty and not hidden by Later), `contains_focus() -> bool`; `Ui.memory` is a `MemoryPanel` added to `stage` as an overlay child (removed from `chat_wobble_box`); `Ui::move_memory(Point)`, `Ui::chat_bounds() -> Option<Rect>` (the chat panel's allocated rect in stage coordinates, or the shell's in glass mode); `SharedState.memory_side: VSide`, `Hover.memory: bool`.

Layout of the panel (width `MEMORY_WIDTH = 360`):

```
┌ Memory · 1 of 3            ▲ ▼   Later  [Approve all] ┐
│ Lumen project                                          │  title 16px bold
│ Added the first KPI: monthly recurring revenue         │  summary 14px gray
│ ┌────────────────────────────────────────────────────┐ │
│ │   Lumen uses DuckDB for analytics.                 │ │  diff 14px, own scroller,
│ │ + The first dashboard KPI is MRR.                  │ │  max 180px
│ └────────────────────────────────────────────────────┘ │
│                            [Deny] [Edit] [Approve]     │  buttons 14px, always visible
└────────────────────────────────────────────────────────┘
```

- Pages are a `gtk4::Stack` (`vhomogeneous(true)`, `SlideUp`/`SlideDown`, 220 ms), rebuilt on `set_items`; the visible child follows `TrayState.page`.
- An `EventControllerScroll` (vertical, bubble phase) on the panel root accumulates `dy`: a discrete wheel step pages at once; smooth (touchpad) pages at |Σdy| ≥ 40 px; then ignores further scroll for 250 ms so one flick moves one page. ▲ ▼ buttons page too (insensitive at the ends).
- "Accept" is relabeled "Approve" (Action::Accept label only; the protocol still says `accept`).
- Shown when `chat_open && panel.is_wanted()` (CSS `open` class with the same fade/scale-in as the chat panel); hovering it calls `set_memory_hover`, which feeds `Hover::is_hovered` like the panel; `should_show_chat` treats focus inside the panel (Edit box) like the chat input being selected.
- `tick.rs`: each frame, if shown: `memory_slot(chat_bounds, panel size, win, side, s.memory_side, s.drag.active)` → `move_memory`; pass its rect as `avoid` to the weather card. `input_rects` adds the panel's bounds while it's shown. In wobble frames the panel gets the same transform as `.chat-wobble` (class `memory-wobble` in the wobble CSS).

- [ ] **Step 1:** Build: `cargo build --release --manifest-path appy-ui/Cargo.toml` → Expected: compiles with no warnings; `cargo test` all pass (Task 4 tests keep the logic pinned).
- [ ] **Step 2: Visual check** with `dev/mock_backend.py --memory` (three sample proposals, one long diff) and the overlay pointed at it (`APPY_WS_URL`/`APPY_PILL_ADDR` on spare ports): screenshot the panel above the chat, page 2 after a scroll, the panel below the chat with Appy dragged to the top, and Edit mode. Expected: title, summary, diff and all buttons fully visible on each page; counter correct; nothing overlaps the weather card.
- [ ] **Step 3: Commit** `feat(ui): memory review as a floating, paged panel above the chat`

---

### Task 6: Docs, mock, deploy, live check

**Files:**
- Modify: `PROTOCOL.md` (memory_settings, memory_auto_approve, kind auto), `README.md` (panel, auto-approve, live notes), `dev/mock_backend.py` (answers `memory_auto_approve`, sends `memory_settings`, a third long proposal), `appy_memory/deploy/deploy.sh` only if needed.

- [ ] **Step 1:** Update docs and mock; `.venv/bin/python -m pytest -q` and `cargo test` → all pass.
- [ ] **Step 2: Deploy** `appy_memory/deploy/deploy.sh` → Expected: Mac tests pass, service healthy, `GET /settings` → `{"auto_approve": false}`.
- [ ] **Step 3: Install the pill**: copy `appy-pill/` over `~/.local/share/plasma/plasmoids/dev.appy.pill/` (it only adds a menu item that stays hidden for an overlay without the field) and restart plasmashell only if Tudor agrees.
- [ ] **Step 4: Live check** from this worktree's `bin/appy`: approve one proposal, then ask Appy about it in the same session (he must know it without a restart); toggle auto-approve from the pill, confirm `[auto]` commits and "Auto-saved" chips; toggle it back off.
- [ ] **Step 5: Commit** `docs: memory panel, auto-approve and live memory notes`; push the branch and open a PR against `feat/memory-review`.
