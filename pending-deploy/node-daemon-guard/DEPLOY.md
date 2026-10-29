# Deploy: node-daemon-guard (wing agent 1.0.39)

## What it is
Wing-side mirror of watchdog-daemon-guard (main-node df-watchdog v15
section 0b). The agent's selfwatch now probes the docker daemon itself
(`docker info`, 15s timeout) before any docker-dependent recovery. While
the probe fails, boot recovery, the per-instance loop, and the disk
auto-prune are skipped; after 2 consecutive failures dockerd restarts
via `systemctl restart docker` (2/hour circuit budget; global
`docker.degraded` marker, auto-clears on a healthy probe). Disk/memory/
loop/log guards keep running during an outage.

## Deploy (rides apply-all — preferred)
```sh
bash pending-deploy/apply-all/deploy-all.sh   # from the repo machine
```
No new stage files: `wing/agent.py` is already in the apply-all stage
list, deployed from repo HEAD. The wing node picks up agent 1.0.39 via
the manager's self-update path on the next heartbeat after the manager
deploy — no installer re-run needed (unlike the old 1.0.2/1.0.3/1.0.4
installer re-runs; the 1.0.24 self-update mechanism covers this).

## Standalone (if apply-all is unavailable)
```sh
scp wing/agent.py root@209.222.10.93:/root/jerusalem/wing-agent.py
ssh root@209.222.10.93 'cp /root/jerusalem/wing-agent.py /root/jerusalem/agent.py 2>/dev/null; systemctl restart jerusalem'
```
NOTE: confirm the manager's on-disk agent path before using the
standalone path — apply-all's vps-apply.sh is the source of truth for
where agent.py lands on the VPS. If unsure, use apply-all.

## Verify (after deploy)
1. `systemctl is-active jerusalem` → `active`.
2. Dashboard Nodes modal: node agent version reads 1.0.39 (or the
   self-update line shows the node picked it up on the next heartbeat).
3. Negative check (do NOT run on a healthy node): none — the guard is
   passive until dockerd actually fails. On a node with a dead dockerd,
   `selfwatch.log` should show `docker daemon unreachable (strike 1/2)`
   and no per-instance `docker start` attempts, then
   `systemctl restart docker` on strike 2.
4. Dashboard: while a node is docker-degraded, its Nodes-modal badge
   reads "needs attention" with the docker-daemon reason (existing
   `selfwatch_state()` degraded map — no dashboard change needed).

## Rollback
Re-deploy the previous agent.py from `/root/jerusalem/.deploy-backup/<ts>/`
via apply-all's rollback path, or re-run apply-all from the previous
commit. The guard is additive: removing it restores the old
docker-down behavior (per-instance strikes), nothing else changes.
