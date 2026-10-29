# Deploy: keepalive-probe-timeout (keepalive v3)

## What it is
The in-container `roblox-keepalive.sh` loop (the thing that keeps Roblox in
the foreground on every instance) could freeze forever inside
`roblox_focused()`: a wedged Android session makes `dumpsys window` hang,
`pgrep` still sees the script alive (so no watchdog ever restarts it),
`keepalive.degraded` is never written (so the dashboard never shows it),
and Roblox sits unfocused indefinitely behind a green dashboard card.
This is the container-side version of the hang class df-watchdog v13
fixed on the host (`dw()` timeout wrapper).

## Changes
- `waydroid-image/roblox-keepalive.sh` v2 -> v3 (only file changed):
  - new `aw()` wrapper: every Android probe (`dumpsys window`,
    `pm path`) and every launch `input` command runs under
    `timeout $KA_PROBE_TIMEOUT_S` (default 10s; toybox `timeout` is
    present in this image — `screencap-loop.sh` already relies on it).
    Falls back to the old unbounded call when the binary is missing.
  - a timed-out probe is UNKNOWN, not "not focused": the loop skips the
    launch cycle (no taps at a wedged session, no launch-budget burn).
  - `KA_PROBE_FAILS` (default 3) consecutive FOCUS-probe timeouts ->
    `keepalive.degraded` with a probe-timeout reason on the first line.
    Surfaces through the existing mirrors with zero manager/dashboard
    changes: df-watchdog v8 check 3b (main node) and wing agent 1.0.15
    check 3b (wing nodes) copy the first 160 chars into the dashboard
    "needs attention" badge.
  - a successful focus probe resets the counter; Roblox focused again
    clears the marker (existing lifecycle). A `pm path` timeout while
    dumpsys answers skips the cycle conservatively without touching the
    counter — only the focus probe determines whether we can verify
    Roblox focus.

## Deploy
No new stage files — `waydroid-image/roblox-keepalive.sh` is already in the
apply-all stage list; rides it from HEAD:
1. `bash pending-deploy/apply-all/deploy-all.sh` (or
   `bash pending-deploy/newbox-sync/sync-newbox.sh --yes` for the new box)
   — stages the new script to `/root/jerusalem/roblox-keepalive.sh`,
   restarts the manager.
2. `bash /root/jerusalem/finish-deploy.sh` on the VPS — step 1
   (`push-keepalive.sh`) copies the staged script into every booted
   `wd-*` data volume and restarts its keepalive loop.
3. New instances get v3 automatically: the Dockerfile already
   `COPY`s it to `/opt/roblox-keepalive.sh` and `boot-complete.sh`
   installs from `$OPT` at every boot.

## Verify
- `bash pending-deploy/keepalive-probe-timeout/test_keepalive_probe_timeout.sh`
  -> `32 passed, 0 failed` (hermetic: fake dumpsys/pm/input with hang
  modes; bounded wall-clock on wedged probes; unknown-skip semantics;
  probe-degraded marker + 160-char mirror contract; recovery clears;
  no-timeout fallback; v2 budget path intact).
- `bash pending-deploy/keepalive-v2/test_keepalive_v2.sh` -> `38 passed,
  0 failed` (regression: crash-loop breaker + v8 mirror behavior unchanged).
- Full `deploy-all.sh --stage-only` preflight stays green (t95 wired).
- Live: after the push, `docker exec wd-rbx1 head -1
  /data/media/0/jerusalem/roblox-keepalive.sh` shows the v3 header, and a
  wedged session surfaces as "Android session unresponsive" on the
  dashboard card within ~40s.
