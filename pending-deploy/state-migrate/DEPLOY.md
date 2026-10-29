# state-migrate — import fleet state into the new VPS

Repo-side package (no staged files — the new VPS is not the apply-all
target). `import-state.sh` runs ON the new box as root, consuming an
archive produced by `backup-state.sh` on the OLD box.

## Why

The fleet's portable data — the Roblox account mapping
(`roblox_accounts.json`), the login-result history
(`roblox_login_results.json`), and the fleet-job journals (`batch_jobs/`)
— lives on the OLD box's state dir (209.222.10.93). When the fleet is
recreated on the new box (64.177.49.68), importing that data keeps the
account mapping without anyone retyping the credential lines.

## Morning SSH-session recipe

Run from any machine with SSH to both boxes (Penta's call — this
script does NOT create instances or touch Docker):

```bash
# 1. Export on the OLD box (209.222.10.93), as root:
ssh root@209.222.10.93 'bash /root/jerusalem/backup-state.sh'
#    (or skip the run and take the newest archive from the 03:17 cron:)
#    ls -t /root/jerusalem-backups/jerusalem-state-*.tar.gz | head -1

# 2. Copy the archive to the new box:
scp root@209.222.10.93:/root/jerusalem-backups/jerusalem-state-<ts>.tar.gz /tmp/

# 3. Copy the importer to the new box and run it (as root):
scp pending-deploy/state-migrate/import-state.sh root@64.177.49.68:/root/
ssh root@64.177.49.68 'bash /root/import-state.sh /tmp/jerusalem-state-<ts>.tar.gz'
```

## What the import does

- Default: imports `roblox_accounts.json`, `roblox_login_results.json`,
  and merges `batch_jobs/*.json` journals (never deletes).
- PRESERVES the new box's identity by default: `instances.json`,
  `providers.json`, `tokens.json`, `static_join_token` are left untouched.
- `--full` additionally imports `instances.json` + `providers.json` —
  ONLY for a full-box swap (those entries point at the OLD host's
  containers; do not use when the new box will create its own fleet).
- NEVER imports `tokens.json` / `static_join_token` (join-token
  continuity for enrolled wing nodes is Penta's decision).
- Safety: member whitelist (fail closed on hostile tar members —
  absolute paths, `..`, unexpected names), snapshot of current dest state
  to `$JERUSALEM_DATA/.pre-import/pre-import-<ts>/` (reversible: copy the
  files back), JSON validation of every imported member, atomic install
  (600 perms files, 700 dirs), `systemctl stop/start jerusalem` (warn-only
  on failure), dashboard 200 probe (warn-only).
- Logs carry names/counts/sizes only — never file contents.

## After the import

Recreate the fleet on the new box from the dashboard ("Create fleet":
prefix `rbx`, next-free-number continues, e.g. rbx1…rbx20) — the imported
accounts then attach to the same names and auto-login covers them at
boot. Then decide with Penta whether to decommission the old VPS.

## Verify

- `bash /root/import-state.sh` (no args) prints usage, exit 2.
- The dashboard shows the fleet coverage ("N/M have accounts") after the
  fleet is created.
- Rollback: stop jerusalem, copy the snapshot files back into
  `/root/jerusalem/`, start jerusalem.
