# wing-update-path

## Problem
`manager/app.py::_agent_disk_path()` resolved the wing agent file with
`dirname(dirname(__file__)) + "/wing/agent.py"`. That matches the repo
(`manager/app.py` -> repo root -> `wing/agent.py`) but NOT the VPS, where
apply-all deploys flat (`/root/jerusalem/app.py` + `/root/jerusalem/wing/`
agent.py). On the live box it pointed at `/root/wing/agent.py`, which does
not exist.

Silent blast radius (nothing raised, nothing logged):
- `current_agent_version()` -> `""`, `agent_file_sha256()` -> `""`
- `agent_update_directive()` -> `None` on every heartbeat -> the 1.0.24
  wing self-update mechanism never fired; nodes stayed on old agents
  forever
- dashboard `/api/providers` `agent_update` badge never appeared
- `GET /api/agent/update/download` -> 500 "agent file unavailable"

Why the tests missed it: every suite used the `_path` overrides on
`current_agent_version`/`agent_file_sha256`, so the real resolution was
never exercised against the deploy layout.

## Fix
`_agent_disk_path()` now prefers `WING_DIR/agent.py` (== the dir the
`/api/install/agent.py` route serves from, so the advertised sha256 and
the served bytes can never disagree) and falls back to the repo layout.
`current_agent_version` docstring updated. No agent change needed.

## Tests
`test_wing_update_path.py` — 20/20 hermetic (fake flat + nested trees,
patched `__file__`/`WING_DIR`, cache resets): A path resolution
(flat/nested/precedence/neither-here/hostile), B flat end-to-end
(version/sha/directive + advertised==served), C repo end-to-end, D wiring
contracts (download + install routes share the path), E old-behavior
regression proof.

## Wiring
Preflight t112 (exact-count `PASS=20 FAIL=0`); header + green-log +
APPLY.md table row + suite lists (112 suites); remote-autologin app.py
snapshot refreshed to HEAD (audit check 4). Rides apply-all from HEAD —
`manager/app.py` already staged, no new stage files.
