# mjpeg-dedup

Send-on-change MJPEG streaming plus a 1s text keepalive ping.

**Problem:** `_handle_ws` re-sent the identical JPEG at 20Hz per viewer.
Static screens (boot, loading, idle game) burned ~20 duplicate frames/sec
of bandwidth for zero visual change.

**Fix:**
- Server sends a binary frame only when it differs from the last sent
  (first frame always sent).
- Server sends a tiny text `"ping"` every `WS_PING_SECS` (1.0s) while the
  source is alive — keeps the dashboard stall badge honest and the socket
  alive through idle proxies. No pings during a real stall.
- Dashboard ignores string messages for the image, still notes the frame
  clock for each.

**Files:** `manager/app.py`, `manager/static/index.html` (both already in
the apply-all stage list — no new stage files).

**Tests:** `test_mjpeg_dedup.py` — 30/30 hermetic. Preflight t44.
