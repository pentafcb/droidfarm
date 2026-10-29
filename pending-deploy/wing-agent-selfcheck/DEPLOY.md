# wing-agent-selfcheck — deploy notes

## What
wing/agent.py 1.0.16 → 1.0.17: the agent probes its own `/api/agent/health`
once per selfwatch pass and requires a complete body (main-node
df-watchdog v5/v12 parity). Two consecutive failed probes → one ALERT log
line + process exit; systemd `Restart=always` (install-wing.sh) revives the
agent in ~5s. Circuit-broken: >2 self-exits/hour → `agent.http.degraded`
marker, alert-only until a healthy probe clears it. Skipped while the server
is not bound yet and when not running under systemd (one WARN per day —
exiting there would orphan the node).

Visibility is automatic: the marker flows through `selfwatch_state()` →
heartbeat → the dashboard Nodes "needs attention" badge; the ALERT /
DEGRADED / recovered lines go to selfwatch.log → the dashboard Eventos
modal. Zero manager/dashboard changes.

## Deploy
Rides the apply-all queue from HEAD (wing/agent.py is already in the stage
list). After the queue deploys, the vultr node needs Penta to re-run the
node installer to pick up agent 1.0.17 (same re-run already pending for
1.0.2–1.0.16); until then srt nodes have no HTTP self-check and nothing
misreports.

Standalone (SSH-capable session, VPS root):
```
scp ~/workspace/droidfarm/wing/agent.py root@<node>:/opt/wing/agent.py   # wing install dir
# then restart the wing service on the node (systemd unit name is the
# per-node random codename, e.g. systemctl restart <codename>)
```
Verify on the node:
```
grep -c sw_check_agent_http /opt/wing/agent.py   # >0
curl -s http://127.0.0.1:8090/api/agent/health   # {"ok": true, "version": "1.0.17", ...}
```

## Verify (after deploy)
- `selfwatch.log` shows no `agent HTTP probe failed` lines on a healthy node.
- Simulate: stop nothing — the check is passive. The exit path is covered
  by the 71/71 hermetic unit checks
  (pending-deploy/wing-agent-selfcheck/test_wing_agent_selfcheck.py).
- Dashboard Nodes modal: no new badge on healthy nodes (marker only
  appears after 3 exits in an hour).

## Rollback
Re-run the installer from the previous agent.py (1.0.16), or restore the
staged backup the installer keeps. The check is additive; 1.0.16 agents
keep working (manager gates accept >=1.0.2).
