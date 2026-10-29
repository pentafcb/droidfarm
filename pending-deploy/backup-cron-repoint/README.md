# backup-cron-repoint

One-shot fix for the 03:17 daily state-backup cron, which still targets the
pre-rename state dir after the 2026-10-08 move to `/root/jerusalem`.

- `repoint-backup-cron.sh` — the VPS-side script. Scans root's crontab and
  `/etc/cron.d/*` for backup lines referencing the legacy pre-rename dir,
  rewrites them to `/root/jerusalem`, self-verifies, idempotent. Staged to
  `/root/jerusalem/repoint-backup-cron.sh` by apply-all; run by hand.
- `test_backup_cron_repoint.sh` — 23/23 hermetic tests (fake cron.d +
  fake `crontab`).
- `DEPLOY.md` — deploy + verify steps.

See `DEPLOY.md` for the full story. Wired into the queue as preflight t34.
