# install-baseline

Fresh `install.sh` runs left the box without the host operational baseline:
no minute-cron `df-watchdog` (all auto-recovery), no daily state backup,
no Docker log rotation. This package closes that gap by having `install.sh`
reuse `baseline-newbox.sh` (the same script the new VPS needed as a manual
run) — warn-only, so a baseline failure can never abort an install.

## Contract
- `install_baseline()` takes the repo root as `$1` (defaults to the script's
  own directory); all four source paths must exist or it warns and skips.
- The baseline is invoked as `bash <bl> <watchdog> <backup-state> <verify-backup>`;
  its own exit code is swallowed after a WARNING.
- No secrets, no instance-data touches (the baseline is read-only except for
  its own installs).

## Tests
`test_install_baseline.sh` — 27/27 hermetic (see DEPLOY.md).
