# agent-resize-numerics-sink-gate — deploy notes

## What it is

The adjacent ungated numerics sink the t171 session named (t170 closed
the agent NAME sinks, t171 closed the create numerics sink):
`resize_instance_linux()` in `wing/agent.py` took `memory_mb`/`cpus`
straight into the docker update argv (`--memory`, `--cpus`) and the node
state record. Its route coerces + bounds today (memory 1024-32768 MB,
cpus 0.5-32, both-None refused), but the sink itself validated
nothing — a future internal caller passing an uncoercible, negative,
fractional-memory, or absurd value would drive docker/state before any
route gate ever ran. (List-form docker argv means no shell injection
here — the failure mode is absurd limits and corrupted state, e.g.
memory 999999999 or cpus "2;rm" stored verbatim.)

The sink now coerces (cpus to float, memory to int; bools rejected,
fractional memory rejected, NaN/Infinity fail the bounds check) and
bounds-checks against the route's validator exactly (memory
1024-32768 MB, cpus 0.5-32), failing closed with `(False, "invalid
cpus/memory")` — or `(False, "memory_mb or cpus required")` when both
are None — before any docker/state touch. Cpus coercion is type-preserving (int stays int, float stays float,
strings coerce to float), so the valid-path argv and state record are
byte-identical to today for every caller. The shrink-below-live-usage guard
(`resize_usage_ok`) stays route-only, as before: it needs live docker
stats and the route already enforces it.

## Files changed (in the repo, deployed from HEAD via apply-all)

- `wing/agent.py` — the gate only. No manager, dashboard, or
  wire-protocol change.
- Deliberately NO agent VERSION bump: the manager's agent gate is
  >= 1.0.2, and bumping would churn 53 test files' VERSION assertions
  for zero behavioral gain. The apply-all MANIFEST hash distinguishes
  the deployed agent.py, VERSION stays 1.0.45.

## Tests

`test_agent_resize_numerics_sink_gate.py` — 101/101 checks (hermetic,
real function, stubbed docker): regression proof, hostile numerics
matrix (cpus + memory, incl. bool/junk-string/NaN/Inf/negative/
fractional-memory/out-of-bounds) refused with zero docker calls and
zero state saves, both-None refused, valid paths unchanged (exact argv,
coerced state, string numerics, fractional cpus 2.5, memory-only and
cpus-only partial resizes, both boundaries, int cpus -> float argv as
the route passes it), docker-failure no-write, hostile name still
gated, gate placement + sibling name/create-numerics/status/delete
sink gates kept + route validator + usage guard kept + deploy wiring,
negative proof.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight
t172). No new stage files — `wing/agent.py` is already in the
apply-all stage list. The vultr node additionally needs Penta to
re-run the node installer so srt1's host gets the new agent.py (same
re-run already needed for agent 1.0.2/1.0.3). Standalone fallback
(only if apply-all is unavailable):

```sh
scp wing/agent.py root@209.222.10.93:/root/jerusalem/wing/agent.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify after deploy

- `systemctl is-active jerusalem` → `active`.
- Dashboard root returns HTTP 200.
- Remote node instance resize still works with normal values (valid
  numerics unchanged); a corrupted resize call can no longer carry
  uncoercible or absurd limits into docker or node state.
