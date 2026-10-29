# cron-ensure

Ensures the `cron` daemon is installed and running wherever Jerusalem's
cron files land.

## Why
Four scripts write cron files but none ensured the daemon exists:

| script | cron file | dead without cron daemon |
|---|---|---|
| `install.sh` step 7 | hourly `trim-docker-logs.sh` (personal crontab) | yes — and worse, the `crontab -l` pipeline fails under `set -e` when the `crontab` binary is missing, aborting the whole fresh install |
| `baseline-newbox.sh` step 3 | minute-cron `df-watchdog.sh` (`/etc/cron.d/jerusalem-watchdog`) | yes — the entire auto-recovery story goes silent |
| `baseline-newbox.sh` step 7 | daily `backup-state.sh` (`/etc/cron.d/jerusalem-backup`) | yes — no backups |
| `finish-deploy.sh` | backup cron-line switch | the files are written but never fire |

## What changed
- `install.sh` — `ensure_cron()` (delimited block, called in step 7):
  fast no-op when already active; otherwise installs the `cron` package
  only when the `crontab` binary is missing, then `systemctl enable --now
  cron` (or `service cron start` without systemd); all failures are
  WARNING + rc 0 (never aborts the install).
- `pending-deploy/newbox-baseline/baseline-newbox.sh` — step 1b
  `ensure_cron_daemon()`: same ensure, warn-only (script `ok`/`warn`
  helpers); a broken apt/systemd warns but never blocks the baseline.
  The install is skipped when `crontab` already exists, so hermetic envs
  with a fake crontab (but no fake apt-get) never reach a real apt-get.
- `pending-deploy/live-verify/verify-live.sh --ssh` — `CRON_DAEMON` probe:
  `active` → OK, else WARN naming the impact.

## Design notes
- The new-box morning run (`baseline-newbox.sh`) now guarantees the daemon
  before the minute-cron watchdog and backup cron files are written.
- The old box (209.222.10.93) has had working crons since 2026-10-06, so the
  probe is expected OK there; the WARN exists for fresh/minimal hosts.
- The wing agent needs nothing: its selfwatch (recoveries, disk guard, log
  trim, drift check) runs in-process, not via cron.

## Tests
`test_cron_ensure.sh` — 44/44 hermetic (fake apt-get/systemctl/service/crontab;
verbatim-extracted product functions). Wired as apply-all preflight t58.
