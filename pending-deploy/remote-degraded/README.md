# remote-degraded

Agent 1.0.22: per-instance selfwatch DEGRADED state surfaces on wing-node
dashboard cards — the twelfth "main had it, wing didn't" parity item (the
manager got the equivalent `instance-health` badge on 2026-10-07).

## The gap
The agent's selfwatch (1.0.3+) writes `<name>.degraded` markers (reason on
the first line) when its circuit breaker pauses auto-recovery — container
restart storms, helper crash loops, stale screencap-loop storms, Waydroid
session-death storms. But `instance_status_linux()` never reported them,
and the manager's `/api/instances` remote branch hardcoded
`"degraded": False` ("watchdog only runs on the main node"). A wedged
remote instance kept showing a green "Ready" card; only the node-level
"needs attention" badge in the Nodes modal knew.

## The fix
- `read_selfwatch_degraded(name)`: marker read with the same name-gate as
  `read_roblox_drift`, reason truncated to 160, never raises.
- `instance_status_linux()` reports `degraded` / `degraded_reason` on both
  branches — rides the 1.0.20 status cache, so both do_GET branches carry
  it with zero route changes.
- Manager: `clean_agent_degraded()` / `clean_agent_degraded_reason()`
  sanitizers (strict-True, str/strip/160-cap); the remote branch fills the
  fields from the agent (older agents omit them → False/None, "no data").
- Dashboard: unchanged — the card badge already reads `i.degraded` with
  priority over the setup/drift badges.

## Tests
`test_remote_degraded.py` — 46/46 hermetic (fake st_dir, fake docker):
VERSION exact 1.0.22 + manager gate, marker units (multiline/truncation/
hostile names/never-raises), both status branches, sanitizer units,
remote-branch source contract, dashboard contract, cache passthrough.
