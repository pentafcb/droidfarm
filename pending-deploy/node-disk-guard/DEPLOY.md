# Deploy: node-disk-guard (wing agent 1.0.7 node disk/log self-recovery)

No standalone scp needed: `wing/agent.py` is already in the apply-all stage
list — this package rides apply-all from repo HEAD.

## What it is
Remote (wing) nodes had no equivalent of the main-node watchdog's disk
auto-recovery (v6 section 8) or container-log trimmer (v10 section 10): a
full Docker data partition on a wing node kills every instance on it plus
the agent's own heartbeats, and Docker's json-file driver never rotates —
each instance container's *-json.log captures all Waydroid session stdout
and can grow to tens of GB (69G was observed on the main node 2026-10-08).

wing/agent.py 1.0.7: the selfwatch thread (every 60s, Linux only) now also
runs two host-level guards once per pass (not per instance):
- `sw_check_disk()`: at >=90% on /var/lib/docker -> SAFE prune only
  (`docker image prune -f` for dangling images + `docker builder prune -f`
  for build cache; NEVER volume prune or `image prune -a` — instance data
  lives in named volumes + the instance image). Budgeted: >2 prunes/hour
  without relief -> `disk.degraded` marker (reason on first line;
  auto-prune pauses, alert-only) until usage drops below 90%, which clears
  it. Freed space logged per run; successful prunes bump recoveries.json.
- `sw_trim_container_logs()`: any `*-json.log` over 1GB
  (`DF_LOG_TRIM_MAX`/`DF_LOG_TRIM_KEEP` env-overridable, default 1GB/1GB)
  is rewritten in place to its last 1GB (same inode — dockerd's open fd
  stays valid; the json-file driver appends at EOF so logging continues).
  Never touches instance data volumes. A failed trim logs one WARN.

Both surface through the existing `selfwatch_state()` degraded map ->
heartbeat -> dashboard Nodes "needs attention" badge. Zero manager or
dashboard changes. All helpers best-effort, never raise.

Deploy (from an SSH-capable session):
```bash
cd ~/workspace/droidfarm
bash pending-deploy/apply-all/deploy-all.sh
```

The deploy scp's `wing/agent.py` -> `/root/jerusalem/wing/agent.py` on the
main VPS and restarts jerusalem. Remote (srt) nodes do NOT get the new
agent automatically: the vultr node still needs Penta to re-run the node
installer so srt's host picks up agent 1.0.7 (same re-run already needed
for 1.0.2 auto-login + 1.0.3 self-watchdog + 1.0.4 remote resources +
1.0.5 drift guard + 1.0.6 adaptive screencap). Until then the srt node
simply has no disk/log guard; nothing breaks and nothing misreports.

Verify:
1. Main VPS: `python3 -c "import sys; sys.path.insert(0,'/root/jerusalem/wing'); import agent; print(agent.VERSION)"` -> 1.0.7
2. Vultr node (after installer re-run): the served agent's `/api/agent/health`
   selfwatch block exists; the node's selfwatch dir has no `disk.degraded`
   under normal usage.
3. Force-check without waiting 60s: on the node,
   `python3 -c "import sys; sys.path.insert(0,'<agent-dir>'); import agent; print(agent.sw_disk_usage_pct())"`
   -> int in [0,100].

Rollback: re-run apply-all from the previous commit (vps-apply.sh restores
timestamped backups automatically on failure).
