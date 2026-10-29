# DEPLOY — remote-node live resource usage (agent 1.0.4)

## What this is
The fleet-resources work (2026-10-08 ~05:45 UTC) showed LIVE RAM/CPU only for
main-node instances: remote instances (srt1.. on the vultr node) reported
`mem_used_mb: None, cpu_pct: None` and their dashboard cards had no usage
bars. The comment even said "remote nodes don't report yet".

Now the wing agent reports per-instance usage and the manager fills it in:

- wing/agent.py (1.0.3 -> 1.0.4): `instance_resources_all()` runs
  `docker stats --no-stream` once per 15s window (cached; Linux only),
  parses with the same wd-* contract as the manager's parser (skips
  malformed lines, non-wd and unsafe names, NaN/inf CPU), drops data older
  than 60s ("no data", never stale lies), never raises.
  `instance_status_linux()` adds `mem_used_mb`/`cpu_pct` to every status
  dict (None when stopped or unavailable) — so they ride the existing
  `/api/agent/instances` fetch, no heartbeat/payload changes.
- manager/app.py: the /api/instances remote branch fills `mem_used_mb`/
  `cpu_pct` via `clean_agent_resource()` (float in [0, 1e6], rounded,
  else None; best-effort, never raises). Older agents (<1.0.4) omit the
  fields -> None, exactly like "no data".
- manager/static/index.html: comment honesty fix only (cards already render
  the usage bar whenever `mem_used_mb != null && memory_mb`).
- Also fixed in the manager's local parser: NaN/inf CPU from docker stats
  is now skipped instead of reaching the dashboard (same one-line guard as
  the new agent parser).

## Files (all ship via apply-all — already in its stage list)
- wing/agent.py (1.0.3 -> 1.0.4) -> /root/jerusalem/wing/agent.py
- manager/app.py -> /root/jerusalem/app.py
- manager/static/index.html -> /root/jerusalem/static/index.html
No new stage files. The node installer serves agent.py from
/root/jerusalem/wing/, so the next `install-wing.sh` run picks up 1.0.4
automatically.

## Deploy (VPS SSH required — blocked in worker sessions)
Option A (recommended):
  bash ~/workspace/droidfarm/pending-deploy/apply-all/deploy-all.sh
Option B (manual, for the record):
  scp wing/agent.py manager/app.py manager/static/index.html \
      root@209.222.10.93:/root/jerusalem-deploy/stage/
  # then run pending-deploy/apply-all/vps-apply.sh on the VPS as root
  ssh root@209.222.10.93 'systemctl restart jerusalem'

## Verify
1. `systemctl is-active jerusalem` -> active
2. Dashboard loads (HTTP 200)
3. On the vultr node AFTER Penta re-runs the node installer (needs agent
   1.0.4): `curl http://<node>:809x/api/agent/instances` (authed) ->
   each instance dict carries mem_used_mb/cpu_pct numbers
4. Dashboard: srt instance cards show "Using X/Y MB · CPU Z%" + RAM bar
   (was absent for remote instances)
5. fleet bar ("Fleet RAM ...") is unchanged — still main-node actuals only

## Rollback
Handled by apply-all/vps-apply.sh (timestamped backups, auto-rollback on
post-install failure). Manual: restore the three files from the pre-deploy
backup and `systemctl restart jerusalem`.

## Notes
- Windows nodes: /api/agent/instances returns 501 (android runtime
  unavailable) — unchanged; no docker-stats code runs there.
- The vultr node still runs whatever agent the last installer run served.
  Penta must re-run the node installer there for 1.0.4 (same re-run already
  needed for 1.0.2 auto-login and 1.0.3 self-watchdog). Until then, srt cards
  keep showing no usage bars — no errors anywhere.
- Tests: pending-deploy/remote-resources/test_remote_resources.py — 43/43
  pass (hermetic fake-docker; no VPS needed). Also in apply-all preflight.
  instance-resources suite updated to the new remote contract (51/51).
- No secrets in code; nothing deleted; restarts only on malfunction.
