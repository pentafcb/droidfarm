# webrtc-loop-supervisor — DEPLOY

Supervises the manager's WebRTC asyncio event-loop thread and makes
`webrtc_offer_sync` fail fast on a dead loop.

## What it fixes

The `webrtc-loop` thread (started by `webrtc_init()`) was the only
long-lived manager background thread NOT covered by the thread
supervisor (2026-10-11). A dead loop thread left `_webrtc_loop`
non-None but dead, so every WebRTC offer hung on
`fut.result(timeout=15)` — 15s per offer, forever, with zero
visibility (no Eventos-modal line, no DEGRADED). WebRTC is first in
the viewer chain, so every new viewer stalled 8s+ before falling back.

Now:
- `main()` registers `"webrtc-loop"` with the thread supervisor via
  the new `_webrtc_loop_thread_factory()` (guarded by `HAS_AIORTC`).
  The supervisor's first pass starts the loop immediately; a dead loop
  is restarted within 30s (max 3/hour, then ALERT to the watchdog log
  → dashboard Eventos modal, same as the other supervised threads).
- The factory always builds a FRESH event loop (a loop whose
  `run_forever()` returned is not trustworthy to re-enter) and closes
  the old one best-effort. Never raises.
- `webrtc_offer_sync()` returns `None` immediately when the loop
  isn't running (`is_running()` is a plain flag read) instead of
  hanging 15s — the dashboard fails over to H.264/MJPEG instantly
  while the supervisor restarts the loop.
- `webrtc_init()` is kept as a thin backward-compatible wrapper.

## Deploy

No new staged files — `manager/app.py` is already in the apply-all
stage list; this rides apply-all from HEAD.

Standalone (SSH session, if ever needed without the queue):

```sh
scp manager/app.py root@HOST:/root/jerusalem/app.py
ssh root@HOST 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```

## Verify

- `systemctl is-active jerusalem` → active
- Dashboard loads (HTTP 200 on :8080)
- A WebRTC viewer connects (or, with aiortc absent, offers fail fast —
  no 15s hangs in the manager log)
- Kill-test (SSH, optional): find the webrtc-loop thread's loop and
  stop it; within ~30s the supervisor restarts it and the watchdog log
  (`/var/log/df-watchdog.log`) shows
  `WARN supervisor started background thread 'webrtc-loop'`

## Tests

`python3 pending-deploy/webrtc-loop-supervisor/test_webrtc_loop_supervisor.py`
— 39/39 hermetic (factory contract incl. hostile/closed old loops,
webrtc_init compat, supervised restart on a fresh loop with the old
one closed, circuit-breaker give-up + single ALERT, offer fail-fast
vs live-loop proceed, main() wiring contract). Wired as apply-all
preflight t59.
