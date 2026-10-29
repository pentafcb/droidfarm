# agent-create-numerics-sink-gate — deploy notes

## What it is

The numerics half of the agent-side sink census (t170 closed the NAME
sinks and named numerics as the adjacent class): `create_instance_linux()`
in `wing/agent.py` took `cpus`/`memory_mb` straight into the docker run
argv (`--cpus`, `--memory`) and the node state record. Its route clamps
today (cpus 1-16, memory 512-32768 MB), but the sink itself validated
nothing — a future internal caller passing an uncoercible, negative,
fractional-memory, or absurd value would drive docker/state before any
route gate ever ran. (List-form docker argv means no shell injection
here — the failure mode is absurd limits and corrupted state, e.g.
memory 999999999 or cpus "2;rm" stored verbatim.)

The sink now coerces (cpus to float, memory to int; bools rejected,
fractional memory rejected, NaN/Infinity fail the bounds check) and
bounds-checks against the route's clamp output exactly (cpus 1-16,
memory 512-32768 MB), failing closed with `(False, "invalid
cpus/memory")` before any docker/state touch. Integral cpus normalize
back to int so the valid-path argv and state record are byte-identical
to today. The tighter manager-side create policy (0.5-32 / 1024+) stays
enforced upstream; the agent sink is the last line against
uncoercible/huge values, not a second policy.

## Files changed (in the repo, deployed from HEAD via apply-all)

- `wing/agent.py` — the gate only. No manager, dashboard, or
  wire-protocol change.
- Deliberately NO agent VERSION bump: the manager's agent gate is
  >= 1.0.2, and bumping would churn 53 test files' VERSION assertions
  for zero behavioral gain. The apply-all MANIFEST hash distinguishes
  the deployed agent.py, VERSION stays 1.0.45.

## Tests

`test_agent_create_numerics_sink_gate.py` — 89/89 checks (hermetic,
real function, stubbed docker): regression proof, hostile numerics
matrix (cpus + memory, incl. None/bool/junk-string/NaN/Inf/negative/
fractional-memory/out-of-bounds) refused with zero docker calls and
zero state saves, valid paths unchanged (exact argv, coerced state,
string numerics, fractional cpus 2.5, both boundaries), gate placement
+ sibling name/ip gates kept + route clamp kept + status/delete sink
gates kept + deploy wiring, negative proof.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight
t171). No new stage files — `wing/agent.py` is already in the
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
- Remote node instance creation still works with normal values (valid
  numerics unchanged); a corrupted create call can no longer carry
  uncoercible or absurd limits into docker or node state.
