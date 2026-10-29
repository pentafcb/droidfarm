# instance-resources — live per-instance RAM/CPU + fleet usage bar

## What it is
The 31GB host carries 20x 2GB instance limits (40GB of limits); limits are not
usage, so nothing showed actual headroom. A new background thread in
manager/app.py polls `docker stats --no-stream` once every 15s (one subprocess
for ALL containers) and caches per-instance {mem_used_mb, cpu_pct}. The cache
goes stale after 60s, so a wedged poller degrades to "no data" instead of lying.
All readers are best-effort and never raise.

- GET /api/instances: local instances gain `mem_used_mb` / `cpu_pct` (null until
  the first poll / on remote nodes); the response gains `fleet_resources`
  {mem_used_mb, cpu_pct, instances_seen, host_mem_mb}.
- Dashboard: each card shows "Using X/Y MB · CPU Z%" + a thin RAM bar (green /
  amber at 70% / red at 90% of the instance limit); the fleet bar shows
  "Fleet RAM 8.2/31 GB · CPU 142% (20 reporting)".

Remote (wing) nodes report null usage for now — the agent could add its own
docker-stats report later; no dashboard breakage either way.

## Deploy
Rides apply-all from HEAD (app.py + static/index.html are already in the stage
list). No new stage files, no new service, no restart of instances.

Standalone fallback (main VPS only):
```
scp manager/app.py manager/static/index.html root@209.222.10.93:/root/jerusalem/  # index.html needs no restart (served from disk)
# app.py needs the restart:
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify
- `systemctl is-active jerusalem` -> active
- Dashboard banner shows no errors; fleet bar reads "Fleet RAM … GB · CPU …%"
  within ~30s of restart (one poller tick).
- Authenticated GET /api/instances shows mem_used_mb/cpu_pct numbers on local
  instances (20x rbx) and nulls on remote ones.
- Open a viewer card: "Using … MB" line + usage bar under the meta line.

## Rollback
Redeploy the previous app.py/index.html via apply-all's automatic rollback
(backups live in /root/jerusalem-backup/<ts>/ on the VPS).
