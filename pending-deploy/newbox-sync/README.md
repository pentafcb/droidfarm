# newbox-sync

One-command code+baseline sync for the NEW VPS (64.177.49.68), which runs the
full Jerusalem 1.0 queue but is not the default `apply-all` target.

`sync-newbox.sh [user@host] [--yes]` runs, in order, aborting on the first
failure:

1. `deploy-all.sh <host> --yes` — full preflight, stage from HEAD, `scp`,
   `vps-apply.sh` on the box (backup → install → syntax gate → single
   restart → verify → automatic rollback).
2. `baseline-newbox.sh` (+ its 3 sources) — minute-cron watchdog, Docker log
   rotation, binder count surface, 03:17 daily backup + drill. Idempotent.
3. `verify-live.sh --host=<bare> --ssh` — file-level SHA check vs HEAD.

See DEPLOY.md for the exact SSH-session commands, verify steps, rollback
notes, and what stays manual (image build, state-migrate, fleet creation).

Tests: `test_newbox_sync.sh` — 45/45 hermetic. Wired as preflight t52.
