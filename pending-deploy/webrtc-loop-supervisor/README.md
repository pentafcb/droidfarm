# webrtc-loop-supervisor

The WebRTC asyncio event loop runs on a dedicated daemon thread
(`webrtc-loop`). It was the only long-lived manager background thread
outside the thread supervisor's watch, so a dead loop silently broke
every WebRTC offer (15s hang each, forever, no log line).

This package:

1. Adds `_webrtc_loop_thread_factory()` in `manager/app.py` — returns
   an unstarted daemon `threading.Thread` named `webrtc-loop` running
   a FRESH event loop (the old one is closed best-effort; a loop whose
   `run_forever()` returned is never re-entered). Never raises.
2. Registers `"webrtc-loop"` with the thread supervisor in `main()`
   (only when `HAS_AIORTC`). The supervisor's first pass starts it
   immediately — no direct `.start()` in `main()`, same pattern as the
   auto-login watcher and resource poller. Death → restart within 30s;
   >3 restarts/hour → give up + one ALERT in the watchdog log (surfaced
   by the dashboard Eventos modal).
3. Makes `webrtc_offer_sync()` fail fast (`None`) when the loop isn't
   running instead of blocking 15s on the future — the dashboard's
   offer path returns 500 immediately and falls over to H.264/MJPEG
   while the supervisor heals the loop.
4. Keeps `webrtc_init()` as a thin wrapper (backward compatible).

## Files

- `manager/app.py` — the change (already in the apply-all stage list)
- `test_webrtc_loop_supervisor.py` — 39 hermetic checks
- `DEPLOY.md` — deploy + verify steps

## Preflight

t59, exact-count grep `39 passed, 0 failed`.
