# backup-visibility — DEPLOY.md

## What it is

Closes the "silent backup" hole: the 03:17 daily state backup ran from
cron with zero dashboard visibility — a failed or never-firing backup was
invisible until a restore was needed.

- `backup-state.sh` writes `$DATA/last-backup.json` on EVERY run (success:
  `{ts, ts_epoch, ok:true, archive, bytes, files, pruned}`; failure:
  `{ts, ts_epoch, ok:false, error}`) via an EXIT trap + `_fail_reason`
  set by `fail()`. Atomic tmp+rename, 600 perms, names/counts/sizes only —
  never file contents.
- `manager/app.py`: `read_backup_status()` (strict field validation, never
  raises → `known:false` when the marker is missing) + auth-gated
  `GET /api/backup-status`.
- `manager/static/index.html`: fleet-bar backup line — green
  `Copia: hace Xh (Y MB)` / red `Copia: falló — revisa` (reason in the
  tooltip) / dim `Copia: sin datos`; 5-minute client-side cache;
  textContent-only; Spanish; no emojis.
- `verify-live.sh --ssh`: WARN-only `BACKUP_MARKER` probe — `ok` → `[OK]`,
  `fail`/`stale`/`missing` → `[WARN]` naming the impact. Never FAILs.

No new staged files: `backup-state.sh`, `manager/app.py`, and
`static/index.html` are already in the apply-all stage list. Rides
apply-all from HEAD like the rest of the queue.

## Deploy

From an SSH-capable session:

```sh
bash pending-deploy/apply-all/deploy-all.sh        # or sync-newbox.sh --yes for 64.177.49.68
```

then on the VPS:

```sh
bash /root/jerusalem/finish-deploy.sh
```

The marker appears after the next 03:17 cron run (or run
`bash /root/jerusalem/backup-state.sh` once by hand to write it now).

## Verify

- Dashboard fleet bar shows `Copia: hace …` (green) after a backup run.
- `curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8080/api/backup-status` → `{"known":true,"ok":true,…}`.
- `bash pending-deploy/live-verify/verify-live.sh --ssh` →
  `[OK] last state backup …` (or `[WARN]` with the reason, never silent).

## Tests

- `bash pending-deploy/backup-visibility/test_backup_visibility.sh` → 17/17
- `python3 pending-deploy/backup-visibility/test_backup_visibility.py` → 33/33
- Preflight t64 runs both (exact-count greps).
