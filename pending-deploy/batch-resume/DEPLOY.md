# batch-resume: resume interrupted fleet jobs ("Reanudar")

## What it fixes

Fleet batch jobs (Start/Stop/Restart/Roblox-all/Login-all, Create fleet) are
journaled, so a manager restart mid-job recovers the partial progress as an
"interrupted" job — but until now the only recovery was re-running the whole
job. Re-running "Restart all" on 20 instances double-restarts the ones that
had already completed.

`POST /api/instances/batch/<job_id>/resume` creates a NEW job covering only
the instances the original job never attempted. The interrupted job stays
pollable as history until the normal 15-min TTL sweep.

## Deploy

Rides `pending-deploy/apply-all` from HEAD — no new stage files
(`manager/app.py` + `manager/static/index.html` are already staged).

Standalone (SSH-capable session only):

```sh
scp manager/app.py manager/static/index.html root@<vps>:/root/jerusalem/
scp manager/static/index.html root@<vps>:/root/jerusalem/static/index.html
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```

## Verify

1. `systemctl is-active jerusalem` → `active`.
2. Dashboard login works; start any fleet action, then `systemctl restart
   jerusalem` mid-job — the progress modal shows "Se interrumpió
   (servidor reiniciado)" with the never-attempted names and a
   "Reanudar (N pendientes)" button.
3. Click Reanudar → the new job runs only the pending instances; its title
   reads "(reanudado)".
4. Resuming a done/running/unknown job → 409/409/404 with a clear message.

## Notes

- Only `interrupted` jobs resume; failed-but-attempted instances are NOT
  retried (they're listed as failed — re-run the action for those).
- Journal-sourced names are sanitized to `^[A-Za-z0-9_-]{1,32}$` before the
  create worker sees them (the worker interpolates names into `shell=True`
  docker calls).
- The dashboard's two duplicated progress-poll loops were unified into one
  shared `pollFleetJob()` — same strings, same ~30s poll-failure tolerance.
