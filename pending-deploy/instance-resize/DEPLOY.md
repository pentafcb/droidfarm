# Deploy: live per-instance RAM/CPU resize

## The gap

Per-instance resource limits (RAM/CPUs) were create-time only: the
single-create endpoint, the fleet-create plan, and the wing agent's
create path all accepted them, but there was **no way to change an
instance's limits after creation** without raw `docker update` on the
VPS. The goal's own description ("assigning RAM and CPU") was only
half-met. With 20 instances at 2GB limits on the 31GB box and actual
usage ~1GB each, live downsizing is the cheapest headroom win there is.

## The fix

- **Manager** (`manager/app.py`):
  - `validate_resize_params()` — each field optional, at least one
    required; bounds match create-time (RAM 1024–32768 MB, CPUs
    0.5–32). Never raises.
  - `resize_local_instance()` — `docker update --memory Nm --cpus C
    wd-<name>` (live, **no restart**); a memory shrink below the
    current live usage (resource poller, 15s cache) is refused with a
    clear 400 (it would OOM-kill the running session); unknown usage
    fails open; `instances.json` is written only after docker accepts
    the limits; the health-cache snapshot is invalidated.
  - `resize_remote_instance()` — same via `wing_request` POST to the
    agent's new route (default 3-attempt budget: user-initiated
    one-shot, per the wing-failfast design).
  - Route `POST /api/instances/<name>/resize` — behind the global POST
    auth gate, route-name-gated (`valid_instance_name` → 404), JSON
    body validated before dispatch.
- **Wing agent 1.0.30 → 1.0.31** (`wing/agent.py`):
  - `resize_instance_linux()` — list-form `docker update` (no shell),
    name re-validated, state persisted only on success, status cache
    invalidated.
  - `resize_usage_ok()` — the same shrink-below-usage guard using the
    agent's own 15s-cached `docker stats`.
  - Route `POST /api/agent/instances/<name>/resize` — agent-key authed,
    `valid_agent_instance_name` → 404, `require_linux` → 501, same
    bounds as the manager. The manager's `>=1.0.2` gates accept 1.0.31.
- **Dashboard** (`manager/static/index.html`): each card gains a
  "Recursos" ghost button next to Eliminar; a modal prefilled with the
  current limits (RAM MB, CPUs) POSTs the resize; Spanish, no emojis,
  `classList.open` modal pattern, errors inline (`rzErr`) or via
  `alert()` like the other card actions.

## What rides apply-all

No new stage files: `manager/app.py` + `wing/agent.py` +
`manager/static/index.html` are already in the apply-all stage list,
and this rides it from repo HEAD. Suite `t100` in preflight:
`pending-deploy/instance-resize/test_instance_resize.py` (77/77:
validation matrix, local happy/failure/guard paths, remote dispatch,
agent 1.0.31 contract, dashboard node-driven contract).

Wiring done: 28 VERSION-tuple suites widened to 1.0.31 (counts
unchanged); wing-update-rollback's exact-bump widened to 1.0.30+
(exact 1.0.31 owned by the new suite); remote-autologin app.py/agent.py
snapshots refreshed to HEAD.

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH:
scp ~/workspace/droidfarm/manager/app.py root@<box>:/root/jerusalem/app.py
scp ~/workspace/droidfarm/wing/agent.py root@<box>:/root/jerusalem/wing/agent.py
scp ~/workspace/droidfarm/manager/static/index.html \
    root@<box>:/root/jerusalem/static/index.html   # served from disk, no restart
ssh root@<box> systemctl restart jerusalem   # needed for app.py/agent.py
# wing nodes pick up agent 1.0.31 via self-update on the next heartbeat
```

## Verify

- `systemctl is-active jerusalem` → active; dashboard loads (HTTP 200).
- Open a card → Recursos → change RAM 2048→1024, Aplicar → card shows
  "RAM: 1024 MB" after refresh; `docker inspect wd-<name> |
  grep -i memory` shows the new limit; no container restart
  (`docker ps` start time unchanged).
- Resize RAM below the card's "Usando X MB" → 400 "below current
  usage" with no change applied.
- Remote instance on a wing node: same flow; the agent's self-update
  carries 1.0.31 on the next heartbeat (Nodes modal badges the update).
