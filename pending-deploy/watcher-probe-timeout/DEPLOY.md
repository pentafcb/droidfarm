# watcher-probe-timeout — DEPLOY

## What it is
Bounds the `waydroid shell` probes in `waydroid-image/booted-watcher.sh`
and `waydroid-image/entrypoint.sh` under `timeout`. A wedged Waydroid
session hangs `waydroid shell` forever; the watcher (a long-lived
background loop) used to freeze on the hung probe, so the late-boot
marker was never written and the dashboard stayed on "booting" until the
watchdog's 15-min stuck-boot restart. The entrypoint's 60x5s boot-wait
loop had the same flaw: a hung first probe defeated the 5-min bound.

## Files changed
- `waydroid-image/booted-watcher.sh` — new `bw()` wrapper (same pattern
  as `roblox-keepalive.sh`'s `aw()`), both probes bounded by
  `BOOTED_WATCHER_PROBE_TIMEOUT_S` (default 30s, env-overridable);
  timeout (rc 124) → WARN + skip cycle, never a marker write.
- `waydroid-image/entrypoint.sh` — boot-wait probe wrapped in
  `timeout 10`; a hung probe retries next iteration, the 5-min bound holds.

## Deploy
Both files ride **apply-all from HEAD**:
- `booted-watcher.sh` is in the apply-all stage list → lands at
  `/root/jerusalem/booted-watcher.sh`, and `finish-deploy.sh` step 2
  (`push-booted-watcher.sh`) pushes it into live containers.
- `entrypoint.sh` is image-side (Dockerfile COPY) → picked up by the
  next `waydroid-instance` image build (`vps-image-build.sh`).

```bash
# from the repo machine, SSH-capable session:
bash pending-deploy/apply-all/deploy-all.sh
# then on the VPS:
bash /root/jerusalem/finish-deploy.sh   # step 2 pushes the watcher live
```

## Verify
- `bash -n` clean on both scripts (preflight syntax gate covers this).
- `pending-deploy/watcher-probe-timeout/test_watcher_probe_timeout.sh`
  → 26 passed, 0 failed (preflight t96).
- On the VPS: `docker exec <wd-c> sh -c 'grep -c "bw waydroid" /opt/booted-watcher.sh'`
  → 2; `grep -c "timeout 10 waydroid" /entrypoint.sh` → 1.

## Rollback
Re-run apply-all from the previous commit; re-run
`bash /root/jerusalem/finish-deploy.sh` to push the old watcher back.
The entrypoint change only affects new images.
