# Deploy: cron-ensure

## What it is
Nothing in the install/baseline path ensured the `cron` daemon is installed
and running — yet four scripts depend on it:

- `install.sh` step 7 (hourly `trim-docker-logs.sh` cron)
- `baseline-newbox.sh` step 3 (minute-cron `df-watchdog.sh` — the entire
  auto-recovery story)
- `baseline-newbox.sh` step 7 (daily `backup-state.sh` cron)
- `finish-deploy.sh` (backup cron-line switch)

On a box without a cron daemon (minimal images don't always ship one) every
one of those cron files is silently dead. Worse, `install.sh` step 7's
`crontab -l | ... | crontab -` pipeline fails under `set -e` when the
`crontab` binary is missing, aborting the whole fresh install outright.

## Changes (repo-side package — no staged files)
- `install.sh`: new `ensure_cron()` (delimited `cron-ensure` block, called at
  the top of step 7) — no-op when `systemctl is-active cron` is already
  `active`; otherwise installs the `cron` package only when the `crontab`
  binary is missing, then `systemctl enable --now cron` (or `service cron
  start` without systemd). Every failure path prints a WARNING and returns
  0 — a failed ensure can never abort the install under `set -e`.
- `pending-deploy/newbox-baseline/baseline-newbox.sh`: new step 1b
  `ensure_cron_daemon()` (delimited `cron-ensure-fn` block) — same ensure,
  warn-only by design (uses the script's `ok`/`warn` helpers): a broken apt
  or missing systemd warns but never blocks the rest of the baseline, and
  the cron files are still written. Header Steps list documents 1b.
- `pending-deploy/live-verify/verify-live.sh --ssh`: new `CRON_DAEMON` probe
  (`systemctl is-active cron`) — `active` → OK, anything else → WARN naming
  the impact (watchdog + backup crons never fire). WARN, not FAIL: it is an
  operational flag for the morning SSH session, not a code-currency check.

## Deploy
Nothing to deploy — repo-side like `live-verify`/`queue-audit`/`binder-verify`:
- `install.sh` is used for future fresh installs (and the pending GitHub push).
- `baseline-newbox.sh` is scp'd by hand per its own DEPLOY.md — the morning
  SSH session's `baseline-newbox.sh` run now ensures cron before writing the
  minute-cron watchdog and backup cron files.
- `verify-live.sh` runs from the repo machine; `--ssh` now flags a dead
  cron daemon on either box.

## Verify
- `bash pending-deploy/cron-ensure/test_cron_ensure.sh` → `44 passed, 0 failed`
  (hermetic: verbatim-extracted functions driven against fake
  apt-get/systemctl/service/crontab — already-active no-op, install+enable
  path, package-present skip-install path, apt-failure warning without
  abort, no-systemd `service` fallback, call-site placement, `bash -n` on
  both scripts, plus the verify-live probe wiring).
- `bash pending-deploy/live-verify/test_live_verify.sh` → `PASS=93 FAIL=0`
  (new T32/T33: inactive/unknown daemon → WARN, exit 0; T9 happy path asserts
  the `[OK] cron daemon active` line).
- Full `deploy-all.sh --stage-only` preflight stays green (t58 wired, t32
  exact-count bumped 88 → 93).

## Tests
`pending-deploy/cron-ensure/test_cron_ensure.sh` (37/37, hermetic — see above).
