# Memory Pills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace one-chip-per-change memory chips with one centred pill per burst that unfolds into the saved facts on hover.

**Architecture:** The brain adds `facts`/`dropped` to `memory_committed`. The overlay keeps the grouping rules in a pure `MemoryBurst` (memory.rs) and draws it in `ChatLog::push_memory` (chat.rs) with a `gtk4::Revealer`.

**Tech Stack:** Python 3.14 (unittest), Rust gtk4-rs 0.9.

**Spec:** `docs/superpowers/specs/2026-11-27-memory-pills-design.md`

## Global Constraints
- `memory_committed` stays backward compatible: `facts`/`dropped` optional (`#[serde(default)]`).
- Burst window 4 s; undos group only with undos; any other chat entry ends a burst.
- Only the newest pill's Undo is live; each works once (as today).
- Tests: `.venv/bin/python -m unittest discover -s tests -t .`; `cargo test --manifest-path appy-ui/Cargo.toml`.

## Review Focus
1. Undo on a 3-change pill must send exactly 3 `memory_undo` and then disable itself. (Task 2 test `undo_count_matches_changes`.)
2. A chat message between two commits must split them into two pills. (Task 3 wiring: `push()` ends the burst.)
3. An old overlay/brain pair: a `memory_committed` without `facts` still renders (summary fallback). (Task 2 test `summary_fallback_when_no_facts`.)

---

### Task 1: Brain sends the facts
**Files:** `ui_bridge.py`, `memory_review.py`; tests `tests/test_ui_bridge.py`, `tests/test_memory_review.py`
**Produces:** `UiBridge.memory_committed(kind, summary, facts=(), dropped=())` → `{"type","kind","summary","facts":[...],"dropped":[...]}`; `emit_committed(kind, summary, facts, dropped)` from MemoryReview for approved/edited/auto (added/removed bullets, without "- "), remember (`[line]`, []), forget (`[]`, `[line]`), undo (`[]`, `[]`).
- [ ] Failing tests: bridge payload; review chips carry facts for accept (`["Korad"]`), auto (`added`/`removed` from the feed), remember/forget tool results.
- [ ] Implement; full suite green; commit `feat(memory): memory_committed carries the facts`.

### Task 2: Burst logic (pure)
**Files:** `appy-ui/src/protocol.rs` (`facts: Vec<String>`, `dropped: Vec<String>` with `#[serde(default)]`), `appy-ui/src/memory.rs`
**Produces:** `pub struct MemoryBurst { .. }` with `new(kind, summary, facts, dropped, now) -> Self`, `accepts(&self, kind, now) -> bool` (within `BURST_WINDOW = 4 s` of the last change and same family: undo vs not), `add(&mut self, kind, summary, facts, dropped, now)`, `title(&self) -> String`, `lines(&self) -> Vec<(String, bool /*dropped*/)>`, `undo_count(&self) -> usize`, `undo_label(&self) -> Option<&'static str>`.
- [ ] Failing tests: `burst_title_counts` ("Remembered a fact", "Remembered 3 facts", "Forgot a fact", "Remembered 2 facts · forgot 1", "Undid 2 changes"), `burst_window_and_family`, `summary_fallback_when_no_facts`, `undo_count_matches_changes` (3 changes → 3; undo burst → 1 with "Undo earlier"), protocol parses `facts`/`dropped` and defaults them.
- [ ] Implement; cargo test green; commit `feat(ui): memory bursts`.

### Task 3: The pill widget
**Files:** `appy-ui/src/chat.rs` (`push_memory(kind, summary, facts, dropped, undo: Box<dyn Fn()>)` replaces `push_chip`; `push()` ends the current burst), `appy-ui/src/events.rs`, `appy-ui/src/style.css`, `dev/mock_backend.py` (sends facts)
- Pill: `GtkBox` vertical, `halign(Center)`; title label in a status-pill; `Revealer` (`SlideDown`, 220 ms) with one wrapped label per line (`memory-fact`, `memory-fact dropped` struck through) and the Undo button; `EventControllerMotion` on the whole pill reveals/hides.
- Undo sends `undo_count()` `ClientEvent::MemoryUndo`s, then disables; older pills' Undo disabled when a newer pill is pushed (as today).
- [ ] Build clean, cargo test green; on-screen check with the mock (a 3-fact burst, a single fact, an undo pill; dropdown opened by hover is not drivable here, so check the revealer by a temporary `set_reveal_child(true)` screenshot, then remove it); commit `feat(ui): one memory pill per burst, unfolding into the facts`.

### Task 4: Docs and live check
- [ ] PROTOCOL.md (`facts`, `dropped`), README (pills); live run with a seeded test proposal approved → one pill with the fact; undo it; vault clean; commit; push to PR #8.
