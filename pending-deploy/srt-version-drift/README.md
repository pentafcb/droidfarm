# srt-version-drift

Wing agent 1.0.5: remote-node Roblox version-drift guard.

The main-node watchdog (df-watchdog.sh section 10) detects a Roblox client
drifted off the pinned build (2.740.931), but remote (srt) nodes had no
equivalent — a force-updated client there was invisible until someone tapped
into the instance.

## Changes
- `wing/agent.py` 1.0.5: `check_roblox_drift()` runs in the selfwatch thread
  (every 60s): `docker exec <c> waydroid shell dumpsys package
  com.roblox.client` (dumpsys only exists INSIDE the container), compares
  versionName to `PINNED_ROBLOX` (env-overridable via `DF_PINNED_ROBLOX`),
  writes `<name>.roblox_drift` (reason on first line) on drift, removes it
  back on pinned. ALERT log line once per drifted version (deduped). Alert
  only: no auto-remediation, no circuit-breaker budget.
- `wing/agent.py`: `instance_status_linux()` surfaces
  `roblox_drift`/`roblox_drift_reason` for running instances
  (False/None when stopped).
- `manager/app.py`: `clean_agent_drift()`/`clean_agent_drift_reason()`
  sanitizers; `/api/instances` remote branch reports the agent's drift
  (older agents omit the fields -> False/None). Dashboard needs no change:
  the drift badge already keys on `i.roblox_drift` for every card.

## Tests
`test_srt_version_drift.py` — 38 checks, hermetic (fake docker).
