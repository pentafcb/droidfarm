# DEPLOY: fleet batch operations (batch-ops)

## What it is
Fleet-wide one-tap actions for the 20-instance farm: the dashboard gains a
"Fleet:" bar (Start all / Stop all / Restart all / Roblox all) backed by a new
async batch engine in the manager.

- `POST /api/instances/batch` {action, names?} -> 202 {job_id} (names omitted
  = all instances; one running job at a time -> 409 otherwise)
- `GET /api/instances/batch/<job_id>` -> progress {status, total, done,
  results, failed}; `DELETE ...` removes finished jobs (15-min TTL sweep)
- Actions: start, stop, restart, launch-roblox — local (docker) and remote
  (wing proxy); sequential per instance (no thundering herd)
- Single-instance endpoints (start/stop/launch-roblox) refactored to share
  `instance_single_action()` with the batch worker — behavior unchanged
- Dashboard: fleet bar + progress modal (1s polling, per-instance failures,
  grid refresh on completion, confirm() on stop/restart)

## Files (ride apply-all from HEAD — no new stage files needed)
- manager/app.py -> /root/jerusalem/app.py
- manager/static/index.html -> /root/jerusalem/static/index.html (served from
  disk per request; no restart strictly needed for the UI, but restart anyway)

## Preferred deploy (from an SSH-capable session)
```bash
cd ~/workspace/droidfarm
bash pending-deploy/apply-all/deploy-all.sh root@209.222.10.93
```
Preflight runs this package's suite (t10) plus all other suites; on success it
scps the files, restarts jerusalem, and verifies.

## Standalone deploy (scp only these two files)
```bash
scp manager/app.py manager/static/index.html root@209.222.10.93:/root/jerusalem/
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && \
  systemctl restart jerusalem && sleep 3 && systemctl is-active jerusalem'
```

## Verify
1. `curl -sk https://<dash>/` -> HTTP 200 (or http://209.222.10.93:8080 per setup)
2. Dashboard (logged in) shows the "Fleet:" bar above the instance grid
3. Start a "Roblox all" batch -> progress modal counts up, failures (if any)
   are named, grid refreshes when done
4. `GET /api/instances/batch/<job_id>` with the dashboard token returns
   {status:"done", total, done, failed:[]}

## Tests
`python3 pending-deploy/batch-ops/test_batch_ops.py` — 51/51 hermetic checks:
single-action local/remote/restart/guards, job lifecycle, validation, 409
concurrency guard, TTL sweep, route-regex placement, dashboard contract.

## Safety notes
- Never touches instance data, credentials, or tokens; stop/restart only act on
  containers (watchdog restarts dead ones the same way).
- Names are validated against instances.json (keys are sanitized at creation);
  no user input reaches a shell unvalidated.
- Auth required on all three batch routes (dashboard token), same as other APIs.
