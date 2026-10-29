# agent-ip-gate

## Problem
`POST /api/agent/instances` accepted an arbitrary `ip` string and
`create_instance_linux()` passed it verbatim to `docker run --ip`
with zero validation — the name was fullmatch-gated, the ip was not.
The agent's `docker()` is list-form, so this was never shell
injection here, but it failed cryptically inside docker run and left
the boundary open for any future shell-form sink.

## Fix (wing/agent.py only)
- `valid_agent_ipv4()`: strict dotted-quad via `ipaddress.IPv4Address`,
  never raises, no stripping (the validated value must be exactly the
  value docker receives).
- `create_instance_linux()`: invalid ip → `(False, "invalid ip")`
  before any docker call. Name gate still fires first.

## Why no version bump
Valid-input behavior is unchanged: the only caller (the manager's
remote-create path) always sends a generated `10.20.0.N` quad. A bump
would cascade through ~35 VERSION-membership suites for zero behavior
change. The gated agent still reports 1.0.38, so the manager's login
gate (>= 1.0.38) keeps passing and the self-update path delivers the
fix on the next heartbeat after the manager deploy.

## Tests
`test_agent_ip_gate.py` — 66 checks: valid_agent_ipv4 units (valid
quads, malformed, hostile shell payloads, non-string/never-raises),
create wiring (hostile ip → "invalid ip" + docker never invoked +
nothing saved; name-gate precedence; valid ip → docker run with `--ip`
as its own argv + saved state carries the exact ip; manager-generated
range passes; already-exists short-circuit), source contracts (gate
precedes docker run construction, handler still delegates).

## Wiring
Preflight t120, exact-count `PASS=66 FAIL=0`. No new stage files —
`wing/agent.py` rides apply-all from HEAD. The remote-autologin
staged `agent.py` snapshot is refreshed to HEAD (queue-audit check 4).
