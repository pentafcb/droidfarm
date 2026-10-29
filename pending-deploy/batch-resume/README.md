# batch-resume

Resume for interrupted fleet batch jobs.

## Manager (`manager/app.py`)

- `batch_resume_job(jid)` → `(http_code, dict)`:
  - 404 unknown job · 409 job not interrupted / another job running /
    nothing left to resume / job type not resumable · 202 + new `job_id`.
  - New job covers only never-attempted names (failed-but-attempted are not
    retried). `resumed_from` links it to the original job; both are
    journaled.
  - Fleet actions go through `batch_create_job` (same validation/worker);
    `create-fleet` resumes reuse the original plan params via the new
    `_start_fleet_create_job()` (extracted from `fleet_create_job`,
    behavior unchanged).
  - Names from the journal are sanitized (`^[A-Za-z0-9_-]{1,32}$`) —
    a corrupt/hand-edited journal can never inject shell metacharacters
    into the worker's docker calls.
  - Instances deleted (or, for create-fleet, created) meanwhile are
    filtered out rather than failing the whole resume.
  - Best-effort, never raises.
- Route: `POST /api/instances/batch/<16-hex>/resume` in `do_POST`
  (behind the global auth gate).

## Dashboard (`manager/static/index.html`)

- The two duplicated progress-poll loops (fleet actions + fleet-create)
  are now one shared `pollFleetJob(jid, titleLabel, words, total)` —
  identical strings and the same ~30-poll failure tolerance.
- The `interrupted` branch renders a **Reanudar (N pendientes)** button
  when names never ran; `resumeFleetJob()` POSTs `/resume` and polls the
  new job (title gains "(reanudado)"). Spanish, no emojis, names via
  `esc()`.

## Tests

`test_batch_resume.py` — 59 checks, hermetic:
- resume validation (404/409 matrix), happy paths for both job types,
  concurrency guard, deleted-instance filtering, hostile-name sanitization,
  garbage job dicts (never raise), route auth placement, dashboard
  contract, and a node-driven run of the real poller block (interrupted →
  resume → done, 30-failure tolerance, create wording).
