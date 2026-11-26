# Startup from the panel pill: design

Short design, approved in conversation on 2026-11-26. It extends PR #4 ("show booting").

## Intent
Starting Appy should be one click and visibly in progress. The panel pill is always in the Plasma panel, even when Appy isn't running. Clicking it starts Appy: the mascot pops out and shows the loading bar until he's ready. Today, the pill only exists while the overlay is running and hidden, Appy has to be started from a terminal, and boot is mostly invisible.

## Approaches considered
1. **The pill launches the existing `bin/appy`, and the overlay shows booting from its first frame. Chosen.** No new process manager. The overlay covers the Python import gap that the backend can't, because the bridge doesn't exist yet.
2. A systemd user service, started by the pill or at login. It adds service files and a second way of running Appy, and it doesn't help the overlay show anything earlier. Rejected.
3. Start the Python bridge before the heavy imports, so the backend can announce booting sooner. This splits `main.py`'s import order, and it still misses the time before Python starts. Rejected.

## Design
- **Pill (`appy-pill/contents/ui/main.qml`):** always visible, with three looks.
  - **Off:** the overlay at :8002 doesn't answer. Asleep: closed-eye dashes on the mascot face. Click starts Appy.
  - **Starting:** after a click, until the overlay answers or 60 s pass. The pill shows the loading bar.
  - **Running:** unchanged. The face follows Appy's state, booting shows the loading bar, and a click toggles the overlay.
- **Launch:** Plasma5Support's `executable` engine runs `setsid -f "$HOME/.local/bin/appy" --no-build`, with output going to `~/.cache/appy/appy.log`. It uses the full path because Plasma's PATH may lack `~/.local/bin`. It passes `--no-build` because Plasma's PATH may lack cargo; a terminal `appy` still rebuilds.
- **Overlay:**
  - It starts in `Booting`, with the "loading…" chat status created at startup, and it's visible on every start, with a small hop.
  - The backend's replayed `booting` continues the same status, because `apply_status` keeps a status of the same state. `idle` finishes it as "loaded in Ns".
- **`bin/appy`:** committed, since the pill depends on it. The README already describes it.
- **`dev/mock_backend.py`:** sends `idle` when the overlay connects. Otherwise an overlay that now starts in booting would stay there forever with the mock.

## Non-goals
Autostart at login; a systemd unit; real boot progress; showing the chat card at startup.
