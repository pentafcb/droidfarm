# batch-worker-safety

Two batch-job worker hazards fixed in `manager/app.py` (plus a small
dashboard surfacing change in `manager/static/index.html`).

## The hazards

1. **TTL sweep killed running jobs.** `_sweep_batch_jobs()` (15-min TTL,
   runs on every new job creation) popped jobs purely by `created` time,
   regardless of status. A long fleet job (e.g. "Login all" on 20
   instances — sequential login automation) that outlived the TTL was
   deleted from the registry AND unjournaled the moment anyone started
   another job; the worker then exited early on its next per-instance
   check and the job silently vanished mid-flight — partial work done,
   journal gone, no resume possible, dashboard left 404ing.

2. **Worker threads had no top-level safety net.** `_run_batch_job` and
   `_run_fleet_create` run in bare daemon threads. The per-instance
   bodies are try/except'd, but an exception escaping the
   registry/journal bookkeeping killed the thread and left the job
   `"running"` forever — wedging the one-job-at-a-time 409 guard for the
   whole fleet bar (Start/Stop/Restart/Roblox-all/Login-all/Create fleet)
   until a manager restart (which drops every connected viewer). The
   startup-only batch-journal recovery never fires in a live manager, and
   the batch-resume "Reanudar" button only works on `"interrupted"` jobs.

## Manager (`manager/app.py`)

- `_sweep_batch_jobs()`: skips `status == "running"` jobs — only finished
  history is swept. A running job is never removed from under its worker.
- NEW `_mark_job_interrupted(jid, reason)`: best-effort, never raises —
  flips a live `"running"` job to `"interrupted"` with a server-generated
  reason (truncated 160 chars) and journals it, freeing the fleet-bar
  guard and making the dashboard's existing resume path work.
- Both workers wrapped in a `try/except BaseException` net (BaseException
  so even SystemExit/KeyboardInterrupt mark the job instead of stranding
  it) → `_mark_job_interrupted(jid, "worker error: <Type>")`. Partial
  results stay intact.
- Worker threads now named (`batch-<jid>`, `fleet-create-<jid>`) for
  debuggability.
- Drive-by hardening: `_run_fleet_create` reads `cpus`/`memory_mb`/
  `provider` via `.get` with defaults instead of direct subscript (a
  hand-edited/corrupt journal missing those keys used to KeyError the
  worker — caught by the new net, now avoided outright).

## Dashboard (`manager/static/index.html`)

- The `interrupted` branch of `pollFleetJob` shows the server's
  `interrupted_reason` when present (e.g. "Se interrumpió (worker error:
  RuntimeError): 3/20 completadas"); falls back to the old
  "(servidor reiniciado)" wording when absent. Reason is
  server-generated and assigned via `textContent` — no injection risk.

## Tests

`test_batch_worker_safety.py` — 32 checks, hermetic:
- sweep: expired running job survives (registry + journal), expired
  done/interrupted swept, fresh running untouched.
- `_mark_job_interrupted` contract: running→interrupted + reason,
  journaled, non-running/unknown/garbage-shape never raise, 160-char
  truncation.
- worker crash (Exception AND SystemExit) → interrupted, reason names
  the escaping exception, partial results intact, journaled, no password
  material.
- fleet-create worker crash → interrupted.
- crash frees the fleet-bar guard (new job 202s, no zombie 409) and the
  crashed job is resumable via `batch_resume_job` (only never-attempted
  instances run).
- healthy worker still completes → done (regression).
- thread naming + dashboard contract (reason wiring, fallback kept,
  textContent, Spanish).
