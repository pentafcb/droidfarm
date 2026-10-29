# wing-update-path — DEPLOY

The manager's wing self-update chain was silently dead on the VPS: fix the
agent-file path resolution so heartbeats advertise updates again.

## What changed
- `manager/app.py`: `_agent_disk_path()` used
  `dirname(dirname(__file__)) + "/wing/agent.py"` — correct only in the
  repo (`manager/app.py` -> repo root -> `wing/`), but every VPS deploy is
  FLAT (`/root/jerusalem/app.py`, agent staged at
  `/root/jerusalem/wing/agent.py`), so on the live box it resolved to the
  nonexistent `/root/wing/agent.py`. Now prefers `WING_DIR/agent.py` (the
  same dir the `/api/install/agent.py` route serves from, so the
  advertised sha256 can never disagree with the served bytes) and falls
  back to the repo layout. All override-free callers
  (`current_agent_version`, `agent_file_sha256`, the heartbeat directive,
  the `/api/agent/update/download` route) go through it; never raises.
  `current_agent_version` docstring updated for the two layouts.
- `pending-deploy/wing-update-path/test_wing_update_path.py`: 20/20
  hermetic checks (flat/nested/precedence resolution, end-to-end
  version+sha+directive on both layouts, advertised==served agreement,
  download/install route wiring contracts, old-behavior regression proof).

## Impact
Before this fix: on the VPS `current_agent_version()` returned `""`, so
`agent_update_directive()` returned `None` on every heartbeat — wing nodes
NEVER self-updated, the dashboard "update available" badge never fired,
and `/api/agent/update/download` 500'd ("agent file unavailable"). Every
earlier entry claiming "vultr gets agent 1.0.x via self-update on the next
heartbeat" was false on the live box. After deploy, the next heartbeat
from an outdated node carries the directive and the node self-updates
(Linux + systemd only, per the 1.0.24 gates; 1.0.30 rollback still guards
a bad update).

## Deploy
Rides apply-all from HEAD — `manager/app.py` is already in the stage
list. No new stage files. No wing agent change (the agent side was
correct; it just never received a directive).

## Verify
- `python3 pending-deploy/wing-update-path/test_wing_update_path.py`
  -> `PASS=20 FAIL=0`.
- After deploy, on the VPS:
  `python3 -c "import sys; sys.path.insert(0,'/root/jerusalem');
  import importlib.util;
  s=importlib.util.spec_from_file_location('a','/root/jerusalem/app.py');
  m=importlib.util.module_from_spec(s); s.loader.exec_module(m);
  print(m._agent_disk_path(), m.current_agent_version())"`
  -> `/root/jerusalem/wing/agent.py 1.0.37` (or current VERSION).
- Dashboard Nodes modal: an outdated node shows the update-available
  badge; after its next heartbeat the agent self-updates (watch
  `/api/agent/selfwatch-events` on the node for the ALERT line).
