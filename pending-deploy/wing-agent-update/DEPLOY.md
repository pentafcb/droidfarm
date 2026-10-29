# wing-agent-update — agent update-available badge in the Nodes modal

## Gap
The dashboard's Nodes modal showed each wing node's agent version but never
said whether it was CURRENT. The vultr installer re-run has been pending
since agent 1.0.2; the only reminder was a PROGRESS.md line — nobody watching
the dashboard could see the node was 19 versions behind.

## What it does
- `manager/app.py`: `current_agent_version()` reads `VERSION` from the
  deployed `wing/agent.py` (same relative layout in repo and on the VPS:
  `manager/app.py` + `wing/agent.py`), so the "current" version is read
  from disk, never hard-coded — a VERSION bump can never go stale here.
  Cached per process (a deploy restarts the manager); `""` = unknown.
  `agent_update_available()` compares with strict dotted parsing: True when
  the node's agent is older, False when current/newer, None when either side
  is unknown. All helpers best-effort, never raise.
- `GET /api/providers` (auth-gated, existing route): every provider gains
  `agent_latest` + `agent_update` (true/false/null). The local
  pseudo-provider gets `agent_update: null` (it is not a wing agent).
  New fields are additive — backward compatible.
- `manager/static/index.html`: the Nodes modal shows an amber
  "actualización del agente (1.0.4 → 1.0.21)" badge on outdated nodes
  (tooltip names the fix: re-run the node installer); the node Info modal
  shows an "Actualización del agente" line. Spanish, no emojis,
  textContent-only/esc()-escaped.

## Deploy
Rides apply-all from HEAD (no new stage files — `app.py` + `index.html`
are already in the stage list). Verify after deploy:

1. Open the dashboard Nodes modal on a node whose agent is older than the
   deployed one: the amber badge shows with both versions.
2. Info modal on that node: the "Actualización del agente" line shows.
3. A node on the current version: no badge. Local node: no badge.

## Tests
`python3 pending-deploy/wing-agent-update/test_wing_agent_update.py`
— 43/43 hermetic (version parse units, True/False/None contract, fake +
real VERSION file reads, /api/providers wiring contract, dashboard
source contract + node-driven badge rendering incl. XSS escaping).
