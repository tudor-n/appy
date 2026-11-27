# Memory review panel + live memory updates — design

Date: 2026-11-27. Builds on phase 5 (`feat/memory-review`, PR #7). Branch `feat/memory-panel`.

## Why

Tudor tried the phase-5 review tray. It's squeezed inside the 290 px chat panel between
the messages and the input (capped at 260 px), so text is small and buttons get cut off.
He wants the review to feel clean and modular, like the weather card: its own floating
panel, big enough to read one proposal at a time with its buttons fully visible.

Separately, approving a proposal doesn't reach Appy in the running session: his memory
(core + note index) is put in the system prompt once at startup. An approved change to an
existing note only shows up if he happens to `memory_read` it; new notes and core changes
wait for a restart. Tudor wants memory to feel seamless (path 2), so approvals should
reach him right away.

## Goals

1. **Memory panel**: a floating panel, separate from the chat panel, placed above the chat.
   - Larger than the old tray (wider than the chat, bigger font) so one proposal reads
     comfortably: title, summary, the diff, and its buttons (Approve / Edit / Deny), all
     fully visible without scrolling inside the card.
   - One proposal per page. Scrolling (wheel or touchpad) moves to the next one with a
     smooth snap; a "1 of 3" counter shows how many wait.
   - Header actions: **Approve all** and **Later**.
     - Approve all = accept every proposal that is actionable right now (stale ones go
       through the existing refresh path).
     - Later = hide the panel without deciding anything; it comes back when a new
       proposal arrives or on the next launch.
   - Responsive like the chat and the weather card: it sits above the chat, and when
     there isn't room above (Appy dragged near the top of the screen) it moves below the
     chat. Hysteresis while dragging, like the chat's left/right flip, so it doesn't
     flicker at the boundary. It follows the chat's left/right side too.
   - Shown while the chat is open and there are proposals (and not dismissed with Later).
     Edit, errors, stale/refreshing states and the "buttons disabled until the Mac
     answers" behavior all carry over from phase 5 unchanged.
2. **Live memory updates**: whenever a memory change commits during a session (approve,
   edit, undo of a review; the explicit remember/forget tools already happen inside the
   conversation), Appy is told on his next turn: a short `[Memory updated: …]` line with
   the change is prepended to the next user message. The system prompt stays as built
   at startup, so the KV cache isn't rebuilt.

## Non-goals

- No "deny all" (one misclick would throw everything away).
- Chips (`memory_committed` + Undo) stay in the chat log as they are.
- No change to the service, the consolidator, or the protocol between brain and overlay
  beyond what Approve all needs (it can be sent as one decision per proposal).
- Not rebuilding the system prompt mid-session.

## Approaches considered

- **A. Above the chat (picked by Tudor)**, flipping below it when there's no room above.
  Reads as part of the chat, has room to be wide.
- B. Beside Appy, opposite the chat: the weather card's spot, so the two would fight
  for it, and near a screen edge it gets pushed above Appy anyway.
- (Status quo: keep the tray inside the chat panel and just enlarge it — rejected, the
  chat panel's height is what cuts it off.)

## Design calls (mine, correctable)

- Panel width ~360 px, aligned with the chat panel's outer edge (the edge away from
  Appy); body font ~15 px, title ~16 px. Card height sized to fit the largest of title +
  summary + a diff of a few lines + buttons; a long diff scrolls inside its own area so
  the buttons never leave the card.
- Paging: a vertical scroller with one card per page that snaps to the nearest card when
  scrolling stops; the counter updates with the page.
- Approve all: sequential accepts through the existing `memory_decision` path; the panel
  shows them going through as with single accepts. Edit in progress is not touched by
  Approve all (that item is skipped).
- Later: overlay-only state; nothing is sent to the brain or the Mac.
- The panel is part of the overlay's input region (clickable), like the chat.
- Live updates: `MemoryReview` collects one line per commit (summary + slug); `main.py`
  hands them to the agent, which prepends them to the next user turn and clears them.

## Left to the builder

Exact pixel sizes and animation timings (match the weather card's pop-in style), how the
snap is implemented in GTK4 (no libadwaita Carousel), and the wording of the injected
line.
