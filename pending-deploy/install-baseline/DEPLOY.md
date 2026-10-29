# Deploy: install-baseline

## What it is
A fresh `install.sh` run gave a box a working dashboard but **no auto-recovery
and no backups**: the minute-cron `df-watchdog.sh` (the entire watchdog
story, v4–v13) and the daily `backup-state.sh` cron were installed only by
`baseline-newbox.sh` — the new VPS needed it as a separate manual run on
2026-10-12. The install script's own `cron-ensure` comment even referenced
"the watchdog minute-cron, the daily backup cron" as if they existed here.
Without this, the next fresh install silently lands a fleet with no
self-healing and no state backups.

## Changes (repo-side package — no staged files)
- `install.sh`: new delimited `install-baseline` block + new step 8
  (`[8/9] Installing host baseline (watchdog + backups)`; old step 8 is now
  `[9/9]`, old step 7 `[7/9]`). `install_baseline()` reuses
  `baseline-newbox.sh` (single source of truth) with the canonical sources
  from the repo (`waydroid-image/df-watchdog.sh`,
  `pending-deploy/backup-state/{backup-state,verify-backup}.sh`):
  minute-cron watchdog, Docker log rotation, binder count surface, daily
  03:17 backup cron + drill. Warn-only by design — a missing source or a
  failing baseline prints a WARNING and returns 0, so it can never abort
  the install under `set -e`. (On a fresh box the drill fails closed —
  zero state files yet — and the daily cron lands its first archive at
  03:17 once instances exist.)

## Deploy
Nothing to deploy — repo-side like `cron-ensure`/`live-verify`:
- `install.sh` is used for future fresh installs (and the pending GitHub push).
- No staged files, no VPS SSH, no restart needed for this item itself.

## Verify
- `bash pending-deploy/install-baseline/test_install_baseline.sh` →
  `27 passed, 0 failed` (hermetic: verbatim-extracted block — happy path
  argv order, failing baseline warns but never aborts under `set -e`,
  missing baseline/source skips, no apostrophes, no legacy refs, call-site
  + step labels, `bash -n install.sh`).
- Full `deploy-all.sh --stage-only` preflight stays green (t94 wired).
