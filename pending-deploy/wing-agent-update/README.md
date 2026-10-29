# wing-agent-update

Surfaces wing-agent staleness in the dashboard: the Nodes modal now badges
nodes whose agent is older than the one deployed beside the manager, and the
node Info modal shows the available version. Makes the long-pending
installer re-runs visible instead of a PROGRESS.md note.

- `current_agent_version()` — disk read of `wing/agent.py` VERSION (same
  layout repo/VPS, cached, never hard-coded, never raises).
- `agent_update_available(agent_ver, latest=None)` — True/False/None.
- `GET /api/providers` — additive `agent_latest` + `agent_update` fields;
  local pseudo-provider gets null.
- Preflight t78: `python3 test_wing_agent_update.py` → `PASS=43 FAIL=0`.
