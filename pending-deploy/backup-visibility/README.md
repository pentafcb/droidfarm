# backup-visibility

The daily Jerusalem 1.0 state backup (`backup-state.sh`, 03:17 cron) used
to run with zero dashboard visibility: its log went to
`/var/log/jerusalem-backup.log`, which nothing read. A failed backup or a
dead cron was invisible until someone needed a restore.

This package makes the last backup's status visible:

1. **Marker** — `backup-state.sh` writes `$DATA/last-backup.json` on every
   run (success and failure) via an EXIT trap. Success records
   `{ts, ts_epoch, ok:true, archive, bytes, files, pruned}`; failure
   records `{ts, ts_epoch, ok:false, error}` with the reason from
   `fail()`. Atomic (tmp+rename), 600 perms, names/counts/sizes only —
   never file contents. Re-running after a failure flips the marker back
   to `ok:true`.
2. **API** — `manager/app.py` `read_backup_status()` reads the fixed
   marker path (never from user input), strictly validates field types,
   never raises (`known:false` when the marker is missing/corrupt), served
   by auth-gated `GET /api/backup-status`.
3. **Dashboard** — the fleet bar renders a backup line:
   green `Copia: hace Xh (Y MB)` on success, red `Copia: falló — revisa`
   (reason in the tooltip) on failure, dim `Copia: sin datos` when the
   marker is missing. Refreshed from `refresh()` with a 5-minute
   client-side cache (the marker changes at most once a day).
   textContent-only, Spanish, no emojis.
4. **Verify** — `verify-live.sh --ssh` probes the marker WARN-only:
   `ok:<age>:<MB>` → `[OK]`; `fail`/`stale` (>26h)/`missing` → `[WARN]`
   naming the impact. Never fails the run.

## Tests

- `test_backup_visibility.sh` (17/17, hermetic): marker on
  success/failure, valid JSON, 600 perms, archive-name match,
  single-line, tmp cleanup, failure→success flip, no content leakage,
  t40 backup-state regression (40/40), bash -n.
- `test_backup_visibility.py` (33/33, hermetic): `read_backup_status`
  units (missing/corrupt/non-dict/garbage-type markers), route contract
  (do_GET, auth-before-read, 401 shape), the marked dashboard block
  driven in node (render matrix, 5-min cache, age strings, failure
  tolerance), dashboard contract (placement, wiring, Spanish, no
  emojis), verify-live probe contract (WARN-only mapping).

Preflight t64 runs both suites (exact-count greps). Rides apply-all from
HEAD — no new staged files.
