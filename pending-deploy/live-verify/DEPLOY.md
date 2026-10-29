# live-verify — one-command live-state triage for the Jerusalem 1.0 farm

## Deploy

Nothing to deploy — this package is repo-side only. It rides no stage file;
run `verify-live.sh` directly from the repo. No VPS SSH needed for the HTTP
probes (Part 1); Part 2 (`--ssh`) is for the next SSH-capable session —
worker sessions don't have VPS SSH.

## What it is

`verify-live.sh` answers, in one command: is the VPS reachable, is the
manager answering, and does what's LIVE match this repo's HEAD?

- Part 1 (always): unauthenticated HTTP probes — dashboard reachability,
  served `index.html` sha256 vs repo HEAD, auth-gate check, and
  route-existence probes for the newest queue routes (`/api/roblox-accounts`
  -> 401, `/api/watchdog-events` -> 401, `/ws/h264/rbx1` -> 401/501).
- Part 2 (`--ssh`): one root SSH connection running read-only checks —
  `jerusalem.service` state, sha256 of every staged file vs HEAD
  (`/root/jerusalem/app.py`, `static/index.html`, `h264_stream.py`,
  `wing/agent.py`, `roblox-keepalive.sh`, `booted-watcher.sh`,
  `repoint-backup-cron.sh`, `screencap-loop.sh`, `push-screencap.sh`,
  `backup-state.sh`, `verify-backup.sh`, `finish-deploy.sh`,
  `push-keepalive.sh`, `push-booted-watcher.sh`, `android-optimize.sh`,
  `push-optimizer.sh`, plus
  `/usr/local/bin/df-watchdog.sh`), served agent VERSION, ffmpeg presence
  (warning-only: MJPEG fallback is by design), binder device count,
  running `wd-*` container count, watchdog cron presence, and the backup
  cron's repoint (`/root/jerusalem`, fails on legacy pre-rename `/root/droidfarm`).
  Note: a `--ssh` run that skips any staged file is a BUG — the 2026-10-12
  file-coverage fix added the three missing checks
  (`repoint-backup-cron.sh`, `screencap-loop.sh`, `push-screencap.sh`) and a
  follow-up the same day added `finish-deploy.sh`, so a partial deploy
  failing on exactly those files can never report "all good". Queue-audit
  check 8 now guards this mechanically: every `STAGE_NAMES` entry must be
  probed by `--ssh`.

Output is `[OK]` / `[FAIL]` / `[WARN]` lines plus a `VERDICT:` line.
Exit 0 = live matches HEAD (warnings allowed), 1 = unreachable/down/differs,
2 = usage error. Never prints secrets (only SHAs, versions, counts, paths).

Built after the 2026-10-10 ~13:35 UTC incident: the dashboard went
unreachable from worker sessions and nothing on hand could distinguish
worker-egress filtering from a real outage — or say which of the staged
queue packages were actually live. This is the morning-triage command.

## Verify

```bash
bash pending-deploy/live-verify/test_live_verify.sh   # hermetic, 105 checks
```
