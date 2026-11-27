# Memory pills — design (short)

Date: 2026-11-27. Goes on `feat/memory-panel` (PR #8). Approved by Tudor in chat.

## Why
One chip per memory change ("Remembered: …", "Saved: Added the helmet HUD hardware") clutters
the chat and shows the model's summary instead of the facts. Tudor wants one compact pill per
burst of changes that unfolds into the facts on hover. It should look cleaner.

## Design
- **Burst grouping (picked by Tudor over "per gap in the conversation"):** a memory change that
  arrives within 4 s of the previous one joins its pill, as long as nothing else was added to
  the chat in between. Undos group only with undos.
- **The pill:** centred in the chat log, status-pill grey. Title from counts: "Remembered a
  fact", "Remembered 3 facts", "Forgot a fact", "Remembered 2 facts · forgot 1", "Undid a
  change" / "Undid 2 changes".
- **Hover:** a revealer under the pill slides down (≈220 ms) with the facts as short sentences:
  added ones plain, removed ones struck through; changes without fact lines show their summary.
  It folds back when the pointer leaves.
- **Undo:** at the bottom of the dropdown, only on the newest pill: undoes every change in the
  pill (one `memory_undo` per change; the Mac reverts newest-first). On an "Undid" pill it reads
  "Undo earlier" and undoes one more.
- **Facts:** `memory_committed` gains `facts` (added sentences) and `dropped` (removed
  sentences), both optional lists. The brain fills them from what it already computes for
  Appy's live note (approve/edit: the panel's before/after; auto: `/changes` added/removed;
  remember: the saved line; forget: the forgotten line).

## Non-goals
Per-fact undo of older changes (the Mac only reverts the newest change).
