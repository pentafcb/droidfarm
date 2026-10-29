# backup-cron-repoint — repoint the 03:17 daily state backup to /root/jerusalem

## What it fixes

The daily backup cron (03:17, created 2026-10-06) archives the manager's
state — instances, providers, Roblox accounts, tokens. The 2026-10-08 rename
moved the state dir from the legacy pre-rename location to `/root/jerusalem`,
but the cron was never repointed: it has been silently backing up the wrong
directory ever since (the third rename-residue bug after the boot-status and
screenshot paths, both fixed 2026-10-09).

`repoint-backup-cron.sh` finds backup cron lines (contain "backup",
case-insensitive) in root's personal crontab and `/etc/cron.d/*` that still
reference the legacy pre-rename dir, and rewrites them to `/root/jerusalem`:

- word-boundary aware — `/root/jerusalem-deploy` and similar lookalikes are
  NOT touched (verified by test);
- non-backup lines are left alone;
- idempotent — re-running after a successful repoint is a clean no-op;
- self-verifying — after the rewrite it re-scans and fails (exit 2) if any
  legacy backup line remains;
- never prints secrets (cron lines carry paths and schedules only).

## Files

| Repo path | Deployed to |
|---|---|
| `pending-deploy/backup-cron-repoint/repoint-backup-cron.sh` | `/root/jerusalem/repoint-backup-cron.sh` (via apply-all stage) |

The script is a one-shot maintenance tool: it is staged by apply-all but
**run by hand** on the VPS (like `push-booted-watcher.sh`), so the operator
sees exactly what changed. It is NOT executed by `vps-apply.sh` — keeping a
crontab rewrite out of the automated deploy path limits blast radius.

## Deploy

1. Run the normal queue deploy (this package's script rides the stage):
   `bash pending-deploy/apply-all/deploy-all.sh`
2. On the main VPS as root, run the repoint once:
   ```bash
   bash /root/jerusalem/repoint-backup-cron.sh
   ```
   Expected output: `N backup cron line(s) repointed to /root/jerusalem`
   (or `already repointed (no-op)` on a re-run).
3. If it reports `no backup cron line found anywhere`, the cron was deleted
   at some point — recreate the 03:17 daily backup with the canonical
   script (see `pending-deploy/backup-state/DEPLOY.md`):
   ```
   17 3 * * * root bash /root/jerusalem/backup-state.sh
   ```
   It archives the full current state set
   (`instances.json`, `providers.json`, `roblox_accounts.json`,
   `tokens.json`, `roblox_login_results.json`, `static_join_token`,
   `batch_jobs/` journals) to 600-perm tar.gz archives with 7-day
   retention, self-verified on every run. Do not fabricate a raw `tar`
   command with an explicit file list — that is the pattern
   `backup-state.sh` replaces.

Standalone (without apply-all): `scp` the script to the VPS and run it —
no service restart needed.

## Verify

- Re-run the script: must report `already repointed (no-op)`, exit 0.
- `grep -ri backup /etc/cron.d/ | grep droidfarm` → empty (legacy-guard: the
  pre-rename dir must not appear in any backup cron line).
- Next SSH session: `bash pending-deploy/live-verify/verify-live.sh --ssh`
  — its backup-cron check must report `ok: backup cron targets /root/jerusalem`
  (it fails closed on the legacy path).
- Next 03:17 run: tomorrow's archive must contain the live
  `/root/jerusalem/instances.json` (compare mtimes/sizes).

## Tests

`bash pending-deploy/backup-cron-repoint/test_backup_cron_repoint.sh`
(23/23 hermetic: fake cron.d dir + fake `crontab` binary — repoint,
idempotency, lookalike protection, end-of-line/subpath occurrences, personal
crontab, missing-cron warning, missing-dir tolerance, exit-2 on unreadable
file, no-op when already repointed, no secrets in output, `bash -n` clean).
Wired as apply-all preflight t34.
