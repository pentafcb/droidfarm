# batch-journal — batch-job progress journal + restart recovery

## What it fixes
Fleet batch jobs (Start/Stop/Restart/Roblox-all/Login-all via
`/api/instances/batch`, and "Create fleet" via `/api/instances/fleet`)
lived ONLY in the manager's memory. A manager restart mid-job —
watchdog v5 self-restart, a deploy restart, a crash — silently killed the
job: the dashboard's 1s poll then 404'd with a cryptic error, and nobody
could tell which of the 20 instances had already been processed. For a
"restart all" job that's dangerous (half the fleet restarted, the user
assumes all did).

## What changed
- `manager/app.py`
  - New `BATCH_JOURNAL_DIR` (`<DATA_DIR>/batch_jobs`, 700) + atomic,
    600-perm journal writes (`_journal_batch_job`, best-effort, never
    raises). Journaled: on job creation, after every completed instance
    (both `_run_batch_job` and `_run_fleet_create`), and on completion.
    Journal entries hold only names/actions/results — never credentials.
  - `_unjournal_batch_job` on `batch_delete_job` and in
    `_sweep_batch_jobs` (expired jobs' journals are cleaned up).
  - `_recover_batch_jobs()` runs at startup from `main()`: re-registers
    journaled jobs; any still `"running"` becomes `"interrupted"` with
    `interrupted_reason: "manager restarted"` (partial results preserved);
    `"done"` jobs stay pollable until the normal 15-min TTL sweep.
    Filename is strictly validated (`^[0-9a-f]{16}\.json$`); corrupt,
    mismatched, or non-dict journals are skipped. Never raises.
  - `batch_get_job` view exposes `interrupted_reason` when present.
    The one-job-at-a-time guard still only blocks on `"running"`, so an
    interrupted job never wedges the fleet bar.
- `manager/static/index.html` (both fleet progress poll loops)
  - `status === 'interrupted'` branch: stops polling and shows
    "Se interrumpió (servidor reiniciado): X/Y completadas" plus the
    names that never ran ("Sin ejecutar" / "Sin crear"). Spanish, no
    emojis. The grid refreshes so cards show true state.
  - Poll now tolerates ~30 consecutive failures before giving up, so a
    quick manager restart (deploy, watchdog) shows the recovered
    interrupted state instead of an instant error. A genuine 404
    ("job not found") gets the friendly message "El trabajo se perdió
    (servidor reiniciado o trabajo expirado)".

## Tests
`python3 pending-deploy/batch-journal/test_batch_journal.py`
46/46 hermetic checks: path validation (incl. traversal rejection),
journal on create/per-instance/done, 600 perms, delete + sweep cleanup,
recovery (interrupted vs done, partial progress, defaults, hostile
files skipped, unreadable dir silent), never-raises on I/O failure,
no-password secrecy, fleet-create journaling, concurrency guard
(interrupted doesn't block / running does), dashboard contract.

## Deploy (rides apply-all from HEAD)
No new stage files — `manager/app.py` + `manager/static/index.html`
are already in the apply-all stage list.

Standalone (SSH-capable session only):
```
scp manager/app.py manager/static/index.html root@209.222.10.93:/root/jerusalem/  # index.html -> /root/jerusalem/static/index.html
ssh root@209.222.10.93 'systemctl restart jerusalem'
ssh root@209.222.10.93 'systemctl is-active jerusalem'
```
Verify: start a fleet "Stop all" job, `systemctl restart jerusalem`
mid-job, then `GET /api/instances/batch/<job_id>` shows
`status: "interrupted"` with the completed results; the dashboard modal
shows the Spanish interrupted message with pending names. A fresh boot
with a stale journal dir recovers cleanly (check the journal).

## Rollback
Re-deploy the previous `app.py`/`index.html` from the apply-all backup
set; delete `/root/jerusalem/batch_jobs/*.json` if desired (jobs simply
stop being recoverable — in-memory behavior is unchanged otherwise).
