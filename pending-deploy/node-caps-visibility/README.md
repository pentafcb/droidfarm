# node-caps-visibility — agent 1.0.27 node host caps in the Nodes modal

## The gap
The fleet-create plan preview (agent 1.0.21 `/api/agent/headroom`) could
warn that `waydroid-instance` was never built on a node or that its loop
pool was the stock 8 — but the Nodes modal only showed the agent version
and the update badge. The two facts that decide "is this node ready for
fleet creation" were invisible until someone opened the Create-fleet plan.

## The change

| layer | change |
|---|---|
| `wing/agent.py` 1.0.26 → 1.0.27 | `sw_host_caps()` → `{"loop_devices", "image_present"}` (Linux only; Windows → `None`/`None`); carried on every 30s heartbeat via `build_heartbeat_payload()`. Loop count is a `/dev` listing; image check is one 15s-timeout `docker image inspect` (reuses the 1.0.18 tri-state helper). Never raises. |
| `manager/app.py` | `clean_agent_host_caps()` (strict int bounds 0..4096, strict bool, else `None` per field; never raises) → heartbeat route stores `p["host_caps"]` with an `updated` ts → `/api/providers` emits additive `agent_loop_devices` / `agent_image_present` (`null` for the local pseudo-provider and for older/never-heartbeated agents). |
| `manager/static/index.html` | Nodes modal: "imagen lista" (dim) / "imagen no construida" (amber, tooltip) chip + "loop: N" chip (amber below 60, tooltip). Node Info modal: "Imagen Waydroid" and "Dispositivos loop" lines. Spanish, no emojis, `esc()`-escaped. |

Zero protocol breaks: the heartbeat field is additive (older managers
ignore it; older agents omit it → `null` caps). The update badge from
wing-agent-update keeps working; the self-update (1.0.24+) delivers 1.0.27
to nodes automatically after deploy.

## Tests
`test_node_caps_visibility.py` — 33/33:
VERSION exact 1.0.27 + manager `>=1.0.2` gate; `sw_host_caps` units
(Windows, Linux tri-states, never-raises); heartbeat payload contract;
`clean_agent_host_caps` units (bounds/coercion/never-raises); heartbeat
storage + `/api/providers` emission contracts (remote + local `null`);
dashboard contracts (chips, Info lines, `esc()`, Spanish, no emojis);
node-driven chip state machine (built/missing/low-loop/unknown/local).

## Preflight wiring
`t84` — exact-count `PASS=33 FAIL=0`.
