# binder-verify

Read-only verification of the kernel binder stack on a fleet host, run by
hand before creating instances — the morning-list "binder post-reboot
64-device verification" for the new VPS (64.177.49.68).

## Why it exists
The fleet needs 3 binder devices per instance; the pool must survive a
reboot. The 2026-10-13 incident showed the failure modes: a fresh box with
no `binder_linux` module loaded, and a NUMERIC `devices=64` module
parameter (the parameter is a comma-separated NAME list, not a count).
`verify-binder.sh` checks the whole chain and fails closed with the exact
remediation, so a broken pool is never discovered by a cryptic
"no free binder devices" at create time.

## Files
- `verify-binder.sh` — the check (exit 0 PASS / 1 FAIL with remediation /
  2 usage). Read-only, never touches instance data, never prints secrets.
  Hermetic-testable via `BV_DEVDIR`, `BV_SYS_MODULES`, `BV_MODPROBE_D`,
  `BV_MODULES_FILE`, `BV_BINDERFS_DIR` env overrides.
- `test_verify_binder.sh` — 26/26 hermetic checks (preflight t57).
- `DEPLOY.md` — exact scp/run/verify steps for the SSH session.

## Repo-side package
Not staged to the VPS and not in the `--ssh` probe list (nothing deployed —
same pattern as `live-verify`, `newbox-baseline`, `waydroid-image-build`,
`state-migrate`). Wired into the apply-all preflight so the gate suite
stays green.
