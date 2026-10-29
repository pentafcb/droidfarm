# DEPLOY — agent 1.0.3 node self-watchdog + remote health surfacing

## What this is
Remote nodes (e.g. the vultr box running srt1) had NO auto-recovery: the
main-node df-watchdog.sh cron only watches local instances. When a helper
daemon died inside a remote container (screencap loop, input daemon, roblox
keepalive), it stayed dead — black stream, dead input, Roblox on the home
screen — until a human intervened.

This adds a self-watchdog thread inside wing/agent.py (Linux only, every
60s), mirroring df-watchdog.sh semantics:
- dead helper daemons -> detached `docker exec -d ... waydroid shell sh <script>`
- dead container (2 strikes) -> `docker start`
- stale screencap (>30s old live.png) -> pkill + loop restart
- circuit breaker: >6 container starts/hour, >8 helper restarts/10min,
  >10 stale-loop restarts/hour -> DEGRADED marker (reason on first line),
  auto-recovery pauses until all checks go green again (auto-clears)
- state in <DATA_DIR>/selfwatch/ (event files, .degraded markers,
  recoveries.json, trimmed selfwatch.log)

Visibility:
- agent 1.0.3 `/api/agent/health` and 30s heartbeat payloads now carry
  `selfwatch: {recoveries, degraded: {instance: reason}}`
- manager stores it (sanitized) on the provider record; /api/providers emits
  `selfwatch` per node
- dashboard Nodes modal: red "⚠ needs attention (N)" badge (reason in
  tooltip); node Info modal shows the auto-recovery count

## Files (all ship via apply-all — already in its stage list)
- wing/agent.py (1.0.2 -> 1.0.3) -> /root/jerusalem/wing/agent.py
- manager/app.py -> /root/jerusalem/app.py
- manager/static/index.html -> /root/jerusalem/static/index.html
No new stage files needed. The node-side installer serves agent.py from
/root/jerusalem/wing/, so the next `install-wing.sh` run picks up 1.0.3
automatically. Manager auto-login version gate is >= 1.0.2, unaffected.

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
2. Dashboard loads (HTTP 200); banner shows H.264 state unchanged
3. On the vultr node AFTER Penta re-runs the node installer (needs agent
   1.0.3): `curl -H "Authorization: ..." http://<node>:809x/api/agent/health`
   -> version 1.0.3, selfwatch key present
4. Dashboard Nodes modal shows the node card with no "needs attention" badge
   while srt1 is healthy
5. (Optional, safe) kill a helper in a test instance:
   `docker exec wd-<name> pkill -f input-daemon.sh`; within ~60s the agent
   restarts it and selfwatch.log on the node shows "dead -> restart"

## Rollback
Handled by apply-all/vps-apply.sh (timestamped backups, auto-rollback on
post-install failure). Manual: restore /root/jerusalem/wing/agent.py,
/root/jerusalem/app.py, /root/jerusalem/static/index.html from the
pre-deploy backup and `systemctl restart jerusalem`.

## Notes
- Windows nodes: selfwatch_loop() returns immediately (no Docker Linux
  runtime there). Unchanged behavior.
- The vultr node still runs whatever agent the last installer run served
  (1.0.1, no text input). Penta must re-run the node installer there for
  BOTH 1.0.2 auto-login and this 1.0.3 self-watchdog to take effect.
- Tests: pending-deploy/remote-selfwatch/test_agent_selfwatch.py — 33/33 pass
  (fake-docker unit tests; no VPS needed). Also in apply-all preflight now.
- No secrets in code; nothing deleted; restarts only on malfunction.
