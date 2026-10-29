# agent-body-cap — DEPLOY

Wing agent 1.0.37: `_read_json` request-body cap (manager parity).

## What changed
- `wing/agent.py`: `VERSION` 1.0.36 -> 1.0.37; `Handler._read_json` gains
  `max_bytes=2097152` (2MB, same as the manager's default). Over-cap or
  negative Content-Length returns `{}` WITHOUT reading the body; garbage
  lengths / malformed bodies still fail closed to `{}` (never raises).
  All three `do_POST` call sites keep the default cap — legitimate bodies
  behave exactly as before.
- `pending-deploy/agent-body-cap/test_agent_body_cap.py`: 20/20 hermetic
  checks (version + manager >=1.0.2 gate, over-cap never touches rfile,
  hostile lengths, at-cap boundary, legit bodies unchanged, max_bytes
  parameter, truncated/malformed never raise, call-site + cap-parity
  contracts).

## Deploy
Rides apply-all from HEAD — `wing/agent.py` is already in the stage list.
No dashboard or manager change. Wing nodes self-update from the manager
(1.0.24 mechanism) on the next heartbeat after the manager deploy; until
then nodes keep serving with the old uncapped read and nothing misreports.

## Verify
- `python3 pending-deploy/agent-body-cap/test_agent_body_cap.py`
  -> `PASS=20 FAIL=0`.
- After deploy: `verify-live.sh --ssh` (agent VERSION probe shows 1.0.37
  once the node self-updates).
- Live behavior check (from the repo machine, against a node): a POST with
  a huge Content-Length to an authed agent route returns quickly with the
  route's normal empty-body response (e.g. 400) instead of stalling while
  the agent pages gigabytes into RAM.
