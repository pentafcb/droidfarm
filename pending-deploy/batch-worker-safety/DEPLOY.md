# batch-worker-safety: TTL sweep + worker-thread safety for fleet jobs

## What it fixes

Two real hazards in the fleet batch-job machinery (`manager/app.py`):

1. The 15-min TTL sweep (`_sweep_batch_jobs`, runs on every new job
   creation) deleted jobs by `created` time **regardless of status** — a
   long fleet job (e.g. "Login all" on 20 instances) outliving the TTL
   was removed from the registry *and unjournaled* the moment someone
   started another job; its worker exited early and the job vanished
   mid-flight with partial work done and no resume possible.
2. `_run_batch_job` / `_run_fleet_create` ran in bare daemon threads with
   no top-level safety net — an exception escaping the per-instance
   try/except (registry/journal bookkeeping) killed the thread and left
   the job `"running"` forever, wedging the whole fleet bar behind the
   one-job-at-a-time 409 guard until a manager restart.

Fix: the sweep skips running jobs; both workers get a
`try/except BaseException` net that marks the job `"interrupted"` with a
server-generated reason (journaled, partial results intact) so the fleet
bar frees up and the dashboard's "Reanudar" resume path works. The
dashboard's interrupted branch shows the reason when the server provides
one (fallback: the old "(servidor reiniciado)" wording).

## Deploy

Rides `pending-deploy/apply-all` from HEAD — no new stage files
(`manager/app.py` + `manager/static/index.html` are already staged).

Standalone (SSH-capable session only):

```sh
scp manager/app.py root@<vps>:/root/jerusalem/app.py
scp manager/static/index.html root@<vps>:/root/jerusalem/static/index.html
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```

## Verify

1. `systemctl is-active jerusalem` → `active`.
2. Start "Login all" (or any fleet action) on several instances; while it
   runs, start another fleet action → the first job keeps its progress
   (previously: vanished mid-flight once past the 15-min TTL).
3. Dashboard: an interrupted job now shows its reason, e.g. "Se
   interrumpió (worker error: …)"; a restart-interrupted job keeps the
   "(servidor reiniciado)" wording; "Reanudar" resumes the pending names.
4. `batch_create_job` still 409s while a job is genuinely running.

## Notes

- Reasons are server-generated strings (`"worker error: <Type>"`,
  `"manager restarted"`), truncated to 160 chars, never credentials.
- The journal holds only names/actions/results — unchanged.
- Worker threads are now named `batch-<jid>` / `fleet-create-<jid>`.
