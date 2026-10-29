# verify-heredoc-quoting — remote-SSH heredoc quoting fixes in verify-live.sh

## What it fixes

Two latent bugs in the `--ssh` remote probe heredoc of
`pending-deploy/live-verify/verify-live.sh` (the final verification gate of
the morning's `sync-newbox.sh --yes` flow):

1. **Missing closing quote on the CRON_WD line (REAL, user-facing).** The
   line read
   `echo "CRON_WD=$(... || true)` with no closing `"`, so the remote shell
   kept the quote open across the next line and emitted the literal line
   `echo CRON_DAEMON=active` instead of `CRON_DAEMON=active`. The local
   `rv CRON_DAEMON` (`grep -m1 "^CRON_DAEMON="`) then found nothing, and
   EVERY `--ssh` run printed the bogus warning
   `cron daemon is '' — the minute-cron watchdog and the backup cron never
   fire; baseline-newbox.sh now ensures cron` — a false alarm on the very
   gate that is supposed to certify the box is healthy.
2. **`systemctl is-active X || echo unknown` leaked a stray `unknown` line.**
   `is-active` prints the state word AND exits non-zero when the service is
   inactive-but-present, so the `|| echo unknown` fallback fired too and the
   remote output gained a bare `unknown` line after `SVC=...` /
   `CRON_DAEMON=...`. Harmless to `rv()` (it greps `^KEY=`), but it polluted
   the protocol. Both lines now use the capture-then-default idiom:
   `_svc="$(systemctl is-active jerusalem 2>/dev/null)"; [ -n "$_svc" ] ||
   _svc=unknown; echo "SVC=$_svc"` (same for cron).

## Why the suite missed it

The test faked `ssh` with canned `CRON_DAEMON=active` output — it never
executed the embedded heredoc, so quoting bugs inside it were invisible.
New T38 in `test_live_verify.sh` extracts the REAL heredoc from
`verify-live.sh` (no duplicated copy) and pipes it through a real `sh -s`
with faked systemctl/crontab/docker, asserting: exit 0, `SVC=active`,
`CRON_DAEMON=active`, `CRON_WD=1` as exact k=v lines, no `echo `-prefixed
lines, and every output line matches `^[A-Za-z_][A-Za-z0-9_-]*=`.
The reconstruction of the pre-fix heredoc fails 3 of the 7 checks.

## Changes

`pending-deploy/live-verify/verify-live.sh` only (repo-side script — runs
from the repo machine, never staged to the VPS, so no STAGE_NAMES/KEYS
changes). Tests folded into the existing t32 suite (105 -> 112 checks).

## Deploy

Nothing to deploy. `verify-live.sh` runs from the repo machine; the fix is
effective immediately. Next `verify-live.sh --ssh` run emits clean
`CRON_WD=<n>` / `CRON_DAEMON=<state>` lines and the cron-daemon check reads
the real state instead of a spurious WARN.

## Verify

- `bash pending-deploy/live-verify/test_live_verify.sh` -> `PASS=112 FAIL=0`
- Extract + `sh -s` the heredoc: every line `KEY=value`-shaped, no stray lines
- Full queue: `bash pending-deploy/apply-all/deploy-all.sh --stage-only`
  (queue-audit 0 findings, all 110 suites green)
