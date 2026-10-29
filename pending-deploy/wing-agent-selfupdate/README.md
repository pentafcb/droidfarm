# wing-agent-selfupdate

Wing agent 1.0.24: the agent updates itself from its own manager.

- Manager: heartbeat response advertises `{"agent_update": {"version",
  "sha256"}}` when a newer `wing/agent.py` is deployed beside it; new
  `GET /api/agent/update/download` serves the file (agent-key header auth).
- Agent: on a newer directive — Linux only, only under systemd, one
  attempt per process (10-min cooldown retry on transient failures) —
  downloads (2MB cap), verifies sha256,
  byte-compiles, backs up to `.bak`, atomically swaps, logs ALERT, and
  exits for the supervisor to restart. Never raises.
- Dashboard: no changes (badge clears on next heartbeat; Eventos modal
  shows the ALERT).

This is the last agent version that needs Penta's manual installer re-run
on the vultr node; 1.0.25+ flows automatically within ~30s of deploy.

Tests: `test_wing_agent_selfupdate.py` → `63 passed, 0 failed`.
Wired as apply-all preflight `t81`.
