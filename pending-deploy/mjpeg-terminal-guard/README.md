# mjpeg-terminal-guard

## Gap
The MJPEG fallback's `onclose` handler retried every 3s forever,
unconditionally. That is correct for a socket that upgraded and then
closed (server frameless-guard close after 15s without frames, network
blip) — the designed recovery path. But the server also rejects sockets
*before* the WebSocket upgrade: 401 for an expired token, 404 for a
deleted instance, 409 for a stopped instance (`_handle_ws` in
manager/app.py sends the HTTP error instead of upgrading). A browser
WebSocket never fires `onopen` for those; it fires `onerror` then
`onclose` — and the old handler retried anyway, forever: an infinite
reconnect loop hammering the manager while the viewer sat on a stale
frame with no explanation.

## What changed (dashboard only, `manager/static/index.html`)
- New marker block `mjpeg-terminal-guard`: `MJPEG_MAX_NEVER_OPENED`
  (3), counter `mjpegNeverOpened`, `mjpegResetGuard()`, and
  `mjpegRetryAfterClose(opened)` — true = schedule another attempt,
  false = terminal. An opened-then-closed socket returns true and
  resets the streak; a never-opened close increments it and trips at
  3, writing a Spanish terminal message into `rbxState` (textContent,
  no HTML, no emojis).
- `startMJPEG` tracks a per-socket `mjpegOpened` flag (set in the new
  `onopen` handler) and routes `onclose` through
  `mjpegRetryAfterClose(mjpegOpened)`; the retry delay is unchanged
  (3000ms) and the stale-socket guards (`myWs !== mjpegWs`) are
  unchanged.
- `closeViewer()` calls `mjpegResetGuard()` so a fresh viewer session
  starts with a clean counter (openViewer already calls closeViewer).
- No server change, no new stage files: `index.html` is already in the
  apply-all stage list and is served from disk, so no restart is
  needed for this change alone.

## Deliberate boundaries
- Threshold 3, not 1: a single failed handshake can be a tunnel blip;
  the guard trips only on a streak of never-opened closes.
- Opened-then-closed sockets retry forever, as before: the 15s
  frameless-guard close is the recovery signal for a dead screencap
  loop (the watchdog restarts it within a minute), and capping that
  path would break recovery.
- Terminal stop is per viewer session, not latched: closing and
  reopening the viewer resets the counter — the user always has a
  one-click retry, and the terminal message says so.
- No auto-retry at a slower rate after tripping: a 401/404/409 cannot
  heal on its own, and a slow poll would just be a quieter loop.
- The freshness ("Imagen congelada") badge is untouched: it stays
  honest about zero frames while `rbxState` carries the terminal
  explanation.

## Tests
`test_mjpeg_terminal_guard.py`: 35/35 — node driver over the extracted
block (streak, trip at 3, opened-close reset, post-trip stays stopped,
reset restores retrying, missing element never throws) + dashboard
contract (onopen flag, onclose routing, 3s delay, closeViewer reset,
H.264 -> MJPEG chain intact, Spanish text, no emojis, textContent-only).
Wired as preflight t140.
