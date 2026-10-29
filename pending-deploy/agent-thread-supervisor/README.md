# agent-thread-supervisor — wing agent 1.0.10 in-agent thread supervisor

## What it is
The wing agent's two background threads (`heartbeat_loop`, `selfwatch_loop`)
ran unsupervised: a dead `selfwatch_loop` silently disabled ALL node-side
auto-recovery while heartbeats kept flowing (dashboard showed a healthy node
with silently dying instances); a dead `heartbeat_loop` marked the node
offline (cards flicker to "stopped") while the agent's HTTP server kept
answering. This mirrors the manager-side `register_supervised` contract
(2026-10-11) inside the agent: reaps + restarts dead registered threads
every 30s, circuit breaker (>3 restarts/hour -> give up + one ALERT, needs
a human). Restarts/give-ups append to `selfwatch.log`, so the dashboard
"Eventos" modal surfaces them via the existing
`/api/agent/selfwatch-events` + manager severity tagging — zero
manager/dashboard changes.

Edge cases handled: `heartbeat_loop` returns immediately when the agent is
unconfigured (no `main_url`/`provider_id`/`agent_key`) and `selfwatch_loop`
returns immediately on Windows (selfwatch is Linux-only) — registering
either would burn the restart budget and ALERT on a legitimately idle
thread, so `_supervisor_registrations()` skips both.

## Files changed
- `wing/agent.py` (canonical): VERSION 1.0.9 -> 1.0.10, new supervisor
  section, `main()` registers threads instead of direct `.start()`.

## Tests
`test_agent_thread_supervisor.py` — 29/29 hermetic checks (fake factories,
redirected `SELF_WATCH_DIR`, real manager `watchdog_event_severity` +
`_event_ts` contract).

## Deploy
Rides apply-all from HEAD (`wing/agent.py` already in the stage list).
The vultr node gets agent 1.0.10 when Penta re-runs the node installer
(same re-run already pending for 1.0.2–1.0.9); until then srt nodes run
unsupervised and nothing misreports. Manager `>=1.0.2` gates accept 1.0.10.

Verify after deploy: node heartbeats carry `"version": "1.0.10"`;
`verify-live.sh --ssh` checks the staged SHA.
