# Startup from the Panel Pill: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** the panel pill is always present. Clicking it starts Appy, and the mascot pops out showing the loading bar until he's ready.

**Architecture:** the QML pill launches `~/.local/bin/appy --no-build` through Plasma5Support's `executable` engine. The overlay starts visible and in `Booting`. The backend's replayed status continues or finishes that state.

**Tech Stack:** Rust/GTK4 overlay, Plasma 6 QML applet, bash launcher, Python mock backend.

**Spec:** `docs/superpowers/specs/2026-11-26-startup-design.md`

## Global Constraints
- The pill's launch command is exactly `mkdir -p "$HOME/.cache/appy" && setsid -f "$HOME/.local/bin/appy" --no-build >"$HOME/.cache/appy/appy.log" 2>&1 </dev/null`.
- The "Starting" look ends when `GET :8002/state` answers, or after 60 s.
- The overlay is visible on every start (terminal or pill).

## Review Focus
1. **Pill clicked twice while starting:** must launch only once. `bin/appy` also refuses a second copy because of the ports.
2. **Overlay started with no backend at all:** it stays in booting, which is acceptable. With the mock, it must reach idle (Task 1).
3. **Overlay restarted while the backend runs:** the replayed `idle` must finish the startup booting status (Task 2 test).

---

### Task 1: Mock backend greets with idle
**Files:** modify `dev/mock_backend.py`.
- [ ] After `ws.accept()`, send `{"type":"status","state":"idle","label":"idle"}`. Verify with `python -c "import ast; ast.parse(...)"`, and by connecting the overlay to the mock (manual).
- [ ] Commit.

### Task 2: The overlay starts visible and in Booting
**Files:** modify `appy-ui/src/state.rs` (`SharedState::new`), `appy-ui/src/app.rs` (startup: create the booting status, show the window, hop). Test in `state.rs`.
- [ ] Failing test `state::tests::starts_booting`: `SharedState::new(Instant::now()).state == AppyState::Booting`.
- [ ] Set the initial state to `Booting`. In `App::new`, replace `set_visible(false)` with: show the window; push the "loading" chat status into `active_status` (state `Booting`, started now), the same way `apply_status` does for a new busy state; call `sync_pill`; and kick the wobble for a hop.
- [ ] `cargo test` passes. Commit.

### Task 3: The pill is always there and starts Appy
**Files:** modify `appy-pill/contents/ui/main.qml`.
- [ ] `showPill` is always true.
- [ ] Add `launching` (bool) and a 60 s `Timer` that clears it. While `launching && !connected`, draw the loading bar. While `!connected && !launching`, draw the asleep face (two short horizontal dashes).
- [ ] Draw the booting face (and the launching one) as the loading bar, the same shape as the mascot's: an outline plus a fill with `0.08 + 0.87*(1-exp(-secs/3))`. Busy states other than booting keep the dots.
- [ ] Click handler: if `connected`, POST `/toggle`; else if not `launching`, run the launch command via `P5Support.DataSource { engine: "executable" }` and set `launching = true`.
- [ ] Hide the "Quit Appy" context action when not connected.
- [ ] Check the file is valid QML with `qmllint`, if it's installed; otherwise check it when the reinstalled pill loads (manual).
- [ ] Commit.

### Task 4: Commit the launcher and update the README
**Files:** add `bin/appy` (from Tudor's checkout; its unchanged contents). Modify `README.md`.
- [ ] `git add bin/appy`. In the README, document that the panel pill starts Appy (it uses `~/.local/bin/appy`, so the symlink step is required), and that `appy-pill` needs reinstalling with `kpackagetool6 -t Plasma/Applet -u appy-pill` after changes.
- [ ] Commit, then push to `fix/boot-status`.

### Verification (manual, with Tudor's OK for desktop changes)
- Reinstall the pill and restart plasmashell. Point `~/.local/bin/appy` at `/home/tudor/dev/appy-boot/bin/appy`.
- With Appy stopped, the pill shows asleep. Click it: the pill shows the loading bar, the mascot pops out with the loading bar, it turns idle when ready, and the pill follows.
- Quit from the pill's menu: the pill returns to asleep.
