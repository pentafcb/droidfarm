# adaptive-screencap — idle capture-rate throttling

## Problem
Every instance's in-container screencap loop PNG-encoded the framebuffer at
8fps (sleep 0.12) 24/7 — 20 instances on the 8-vCPU fleet host burning CPU
for nobody. The dashboard cards show no thumbnails; the only consumers of
`live.png` are active stream viewers (MJPEG ws loops, the H.264 broadcaster,
WebRTC — all viewer-driven, all of which only run while a viewer is
attached).

## What changed
- `waydroid-image/screencap-loop.sh` (NEW, canonical): the loop captures
  fast (0.12s) while a viewer is attached, slow (2s) when idle. It decides
  from the mtime of `/data/media/0/jerusalem/stream_wanted`: fresh (<30s) =
  viewer attached. A stale marker (manager/agent restart left it behind)
  degrades to slow mode on its own — a dead manager can never pin the loop
  fast forever. The 2s idle cadence keeps the host watchdog and wing
  selfwatch stale-stream checks (>30s) green. Atomic rename preserved
  (no partial reads / black flicker).
- `waydroid-image/Dockerfile`: COPYs the loop to `/opt/screencap-loop.sh`.
- `waydroid-image/entrypoint.sh`: installs the canonical script into the
  shared data dir at boot (same pattern as keepalive-v2); WARN + legacy
  inline fast-only loop if the image predates the file.
- `manager/app.py`: viewer registry (`_stream_viewers`, refcounted per
  (provider, name)); `_handle_ws` and `_handle_ws_h264` call
  `stream_viewer_open` on attach and `stream_viewer_close` in `finally`
  (touch on first viewer, remove the marker on last close — local
  instances only); `resource_poller` re-touches markers every 15s so
  long-lived viewers stay fast. Remote instances are a no-op here — the
  wing agent tracks its own fetch traffic. All best-effort, never raises.
- `wing/agent.py` 1.0.5 → 1.0.6: the screenshot route records each
  successful serve and re-touches the marker (the manager polls
  `/screenshot` once per frame per viewer, so fetch recency == viewer
  attached); the selfwatch thread removes the marker after 90s without
  fetches. Alert-only hint file — never affects health or budgets.
- `pending-deploy/adaptive-screencap/push-screencap.sh`: one-shot push of
  the new loop into live `wd-*` containers (copy into the data volume the
  watchdog restarts from, restart the loop, verify).

## Tests
- `test_adaptive_screencap.py` — 42/42 (hermetic): manager counting /
  refcount / last-out-clears / garbage-names / remote-noop / retouch /
  10-thread churn; wiring contracts for both ws handlers, finally blocks,
  and the poller; agent VERSION 1.0.6, fetch note + sweep lifecycle,
  route + selfwatch wiring; entrypoint/Dockerfile/loop wiring.
- `test_screencap_loop.sh` — 5/5 (hermetic): runs the REAL loop script
  with fake `screencap`/`sleep` and real `stat`/`date`: fresh marker →
  all-0.12s captures, absent marker → all-2s captures, stale marker →
  all-2s (no fast pin), `live.png` still written when idle.

## Deploy
1. Rides `pending-deploy/apply-all` from HEAD:
   - `screencap-loop.sh` staged → installed to
     `/root/jerusalem/screencap-loop.sh` (canonical on-VPS source).
   - `push-screencap.sh` staged → `/root/jerusalem/push-screencap.sh`
     (manual run, same pattern as `repoint-backup-cron.sh`).
   - `app.py` + `wing/agent.py` already in the stage list (manager needs
     `systemctl restart jerusalem`; the agent ships to vultr with the
     installer re-run).
2. Push to LIVE containers (entrypoint change only affects new containers):
   on the VPS as root, `bash /root/jerusalem/push-screencap.sh` — copies
   the staged file into each booted `wd-*` data volume and restarts its
   loop. Verify: script prints "adaptive loop running" per instance.
   (Even a failed restart self-heals: df-watchdog.sh restarts the loop
   from the replaced file within a minute.)
3. New instances get the adaptive loop automatically after the next
   `waydroid-instance` image rebuild (Dockerfile COPY + entrypoint).
4. Wing node: `srt` instances get agent 1.0.6 when Penta re-runs the node
   installer (same re-run already needed for 1.0.2–1.0.5); until then
   their loops stay fast-always (no marker writer) — correct and safe.

## Verify
- `bash /root/jerusalem/push-screencap.sh` → "adaptive loop running" × N.
- Open a viewer → `stat` the marker in the instance volume shows fresh
  mtime; close the viewer → marker removed within ~15s; loop slows
  (watch `live.png` mtime cadence go 0.12s → 2s).
- `GET /api/instances` unaffected (no new fields; viewer tracking is
  server-side only).
