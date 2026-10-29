# backup-state — self-maintaining daily state backup (replaces the 2026-10-06 explicit file list)

## What it fixes

The 03:17 daily backup cron (created 2026-10-06) archives the manager's state
with an **explicit file list** (`instances.json`, `providers.json`,
`roblox_accounts.json`, `tokens.json`, `static_join_token`). Every state file
added since — `roblox_login_results.json` (login observability, 2026-10-07)
and the `batch_jobs/` fleet-job journals (batch-journal, 2026-10-11) — was
**silently never archived**. A restore from today's backup would lose the
login-result history and every journaled fleet job.

`backup-state.sh` moves the state set into one canonical place
(`STATE_FILES` in the script itself): new state files get added there once
and flow into every future backup — cron lines are never edited again.
It also closes the "backup exists but is corrupt" gap: every archive is
self-verified (re-opened, member-listed, every `.json` JSON-validated, set
checked) before old archives are pruned. `verify-backup.sh` gives the same
checks as a standalone drill tool.

- Archive: `<JERUSALEM_BACKUPS>/jerusalem-state-YYYYMMDD-HHMMSS.tar.gz`,
  600 perms, atomic tmp+rename write, 7-day retention (pruned only after a
  verified write).
- Missing state files WARN by name (never silently skipped); zero state
  files found -> FAIL exit 1, nothing archived, nothing pruned.
- Free-space pre-check (default 100MB); logs carry names/counts/sizes only —
  never file contents (passwords, tokens stay out of logs).

## Files

| Repo path | Deployed to |
|---|---|
| `pending-deploy/backup-state/backup-state.sh` | `/root/jerusalem/backup-state.sh` (via apply-all stage) |
| `pending-deploy/backup-state/verify-backup.sh` | `/root/jerusalem/verify-backup.sh` (via apply-all stage) |

Both are staged only. The cron-line switch is done **by hand** on the VPS
(like `repoint-backup-cron.sh`): crontab edits stay out of the automated
deploy path.

## Deploy

1. Run the normal queue deploy (both scripts ride the apply-all stage):
   `bash pending-deploy/apply-all/deploy-all.sh`
2. On the main VPS as root, replace the 03:17 backup cron line with:
   ```
   17 3 * * * root bash /root/jerusalem/backup-state.sh
   ```
   (keep the old line's schedule if it differs; the command is the part
   that matters). If no backup cron line exists at all, add the line above
   — do not fabricate any other backup command.
3. Run it once by hand and confirm `ok:` output:
   `bash /root/jerusalem/backup-state.sh`
4. Restore drill (recommended monthly): copy the newest archive off the box
   and run `bash /root/jerusalem/verify-backup.sh <archive>` — exit 0 means
   the backup is restorable. Never extract archives onto the live state dir.

Standalone (without apply-all): `scp` both scripts to `/root/jerusalem/`
and do steps 2–4 — no service restart needed.

## Verify

- `bash /root/jerusalem/backup-state.sh` -> `ok: ... files=N ... pruned=M`.
- `ls -la /root/jerusalem-backups/` — newest archive is 600 perms.
- `bash /root/jerusalem/verify-backup.sh <newest archive>` -> exit 0,
  `VERDICT: archive is restorable`.
- `crontab -l` (root) shows the `backup-state.sh` line; the old explicit
  `tar ... -C /root/jerusalem ...` line is gone.
- Next SSH session: `bash pending-deploy/live-verify/verify-live.sh --ssh`
  — its backup check reports `ok: backup cron calls backup-state.sh`.

## Tests

`bash pending-deploy/backup-state/test_backup_state.sh`
(40/40 hermetic: full archive + perms + byte-identical round-trip,
missing-file WARN, zero-state fail-closed without pruning, corrupt-JSON
fail-closed with tmp cleanup, retention window, free-space gate, no secrets
in stdout/log, verify-backup good/bad/missing-core/no-batch_jobs modes,
STATE_FILES sync between the two scripts, bash -n clean).
Wired as apply-all preflight t40.
