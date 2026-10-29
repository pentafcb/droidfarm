# android-optimizer

Real Android OS optimization for the Waydroid instances — replaces the
2026-10-13 ~07:45 UTC stub, which used `settings put` (known broken in this
Waydroid build: AppOps NPE from the root shell, verified 2026-10-06),
`cmd package bg-dexopt-job` (blocked: SecurityException), copied the script
to the host and then `docker exec`'d it inside the container (path mismatch),
and restarted the wrong service name. The stub also broke the queue-audit
gate (unwired test, missing APPLY.md row, legacy unit refs).

## What this package does

1. **Canonical optimizer** (`waydroid-image/android-optimize.sh`): the exact
   working set verified on 2026-10-06, extracted verbatim from the
   entrypoint.sh heredoc into a single source of truth —
   `pm disable-user` (5 bloat packages), `svc power stayon true`,
   `dumpsys deviceidle disable`, `pm trim-caches 999G`. Logs to
   `/data/media/0/jerusalem/optimize.log`. Idempotent.
2. **Image wiring**: `Dockerfile` copies it to `/opt/android-optimize.sh`;
   `entrypoint.sh` installs it from `/opt` at every boot with a WARN +
   legacy-inline fallback for images predating the file (same pattern as
   keepalive-v2 / booted-watcher). The pending new-box waydroid-instance
   image build picks this up automatically.
3. **Live push** (`push-optimizer.sh`, staged to `/root/jerusalem/`,
   also a `finish-deploy.sh` step): copies the canonical script into every
   booted `wd-*` container's data volume, runs it once via
   `waydroid shell`, and verifies a NEW `[optimize] done` marker in
   `optimize.log` (a stale marker from an old run can't fake success).
   Unbooted containers are skipped (entrypoint covers them at boot).

## Honest limits (documented, not attempted)

- Animation scales (`window/transition/animator_duration_scale`) can NOT be
  set in this Waydroid build — `settings put` throws AppOps NPE from the
  root shell on all tables. The optimizer does not try.
- `cmd package bg-dexopt-job` is blocked by SecurityException. Not attempted.

## Deploy

Via apply-all (recommended): the two files ride the normal stage
(`android-optimize.sh` + `push-optimizer.sh` → `/root/jerusalem/`), and
`finish-deploy.sh` runs the push as one of its steps.

Standalone (SSH session on the VPS as root):

```
scp waydroid-image/android-optimize.sh pending-deploy/android-optimizer/push-optimizer.sh root@<host>:/root/jerusalem/
ssh root@<host> 'bash /root/jerusalem/push-optimizer.sh'
```

## Verify

- Per container: `docker exec <c> waydroid shell "tail -3 /data/media/0/jerusalem/optimize.log"`
  ends with `[optimize] done`.
- Spot check: `docker exec <c> waydroid shell dumpsys power | grep -i stayon`
  and `pm list packages -d` shows the 5 disabled packages.
- New image: `docker run --rm --entrypoint sh waydroid-instance:latest -c
  'test -f /opt/android-optimize.sh && echo OK'` after the next image build.

## Remote (wing) nodes

The push script is main-box only (it drives local docker). Wing nodes get
the optimizer through their waydroid-instance image rebuild / node
installer re-run — same as keepalive-v2. Until then nothing misreports:
the entrypoint fallback keeps old images working.
