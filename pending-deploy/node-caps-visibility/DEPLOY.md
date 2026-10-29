# DEPLOY — node-caps-visibility (agent 1.0.27: node host caps in the Nodes modal)

## What
The Create-fleet plan preview (1.0.21 `/api/agent/headroom`) could warn
that the Waydroid image was never built on a node or its loop pool is the
stock 8, but nobody watching the dashboard could see those facts — the 1.0.21
probe only runs at plan time. The Nodes modal showed the agent version and
the update badge, never whether the node was actually fleet-ready.

- `wing/agent.py` 1.0.26 -> 1.0.27: new `sw_host_caps()` →
  `{"loop_devices": int|None, "image_present": True|False|None}` (Linux only;
  Windows reports None/None — honest "no data"; never raises), carried on
  every heartbeat (one `docker image inspect` + one `/dev` listing per 30s
  heartbeat, both timeout-bounded).
- `manager/app.py`: new `clean_agent_host_caps()` sanitizer; the heartbeat
  route stores `p["host_caps"]` (with `updated` ts); `/api/providers` emits
  additive `agent_loop_devices` / `agent_image_present` (None/None for the
  local pseudo-provider and for older/never-heartbeated agents).
- Dashboard Nodes modal: per-node "imagen lista" (dim) / "imagen no
  construida" (amber) chip + "loop: N" chip (amber below 60); the node Info
  modal shows "Imagen Waydroid" and "Dispositivos loop" lines. Spanish, no
  emojis, `esc()`-escaped.

## Deploy
Rides apply-all from HEAD — `wing/agent.py` + `manager/app.py` +
`manager/static/index.html` are already in the stage list. No new stage
files. After the manager deploys, wing nodes pick up 1.0.27 via the agent
self-update (1.0.24+) on the next heartbeat (<=30s); the dashboard Nodes
modal badges the update until then.

Standalone (SSH-capable session only):
```
scp wing/agent.py manager/app.py manager/static/index.html root@<mgr>:/root/jerusalem/   # index.html -> static/
ssh root@<mgr> 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```

## Verify
1. `GET /api/providers` (dashboard-authed) — each wing node carries
   `agent_loop_devices` (int) and `agent_image_present` (bool); the local
   pseudo-provider carries `null`/`null`.
2. Nodes modal: node cards show "imagen lista" + "loop: 64" (or the amber
   variants on an unbuilt image / small loop pool).
3. Node Info modal shows the "Imagen Waydroid" and "Dispositivos loop" lines.
4. Dashboard still fully Spanish, no emojis, JS gate passes.

## Rollback
Re-deploy the previous `app.py`/`index.html`/`agent.py` (apply-all backups
them with timestamps); nodes self-update back down only if the manager's
`wing/agent.py` is also rolled back — otherwise they stay on 1.0.27, which
is forward-compatible (additive heartbeat field).
