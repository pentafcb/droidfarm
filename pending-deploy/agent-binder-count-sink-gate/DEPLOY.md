# agent-binder-count-sink-gate — deploy notes

## What it is

The sw_* helper numerics the t172 session named as the adjacent class
(t170 closed the agent NAME sinks, t171–t172 the create/resize numerics
sinks): the binder pool count was unguarded at both write-driving sinks
in `wing/agent.py`:

- `sw_ensure_binder_devices(count=64)` did `int()` + a floor-only
  check — a huge count looped that many binder-control shell commands.
- `sw_boot_binder_pool(count)` did NO coercion and NO bounds check at
  all — `range(count)` drove the binder-control write loop directly.
  Its count defaults from `SW_BINDER_COUNT`, read from `DF_BINDER_COUNT`
  via `_sw_env_int_min` — floored at 1, never ceilinged — so a bad env
  value would loop the write cycle every selfwatch pass.
- `_sw_prepare_pools_for_start()` built its expected set from
  `range(1, SW_BINDER_COUNT)` with the same unguarded value.

The failure mode is a runaway shell loop (and, for the set build, an
unbounded allocation), not injection — device names are generated
internally and every command is a fixed template.

One shared `_sw_coerce_binder_count()` now bounds the count to
[1, 256] (default pool 64, 4x headroom) at all three sites. Invalid
counts fail closed with zero shell touches: ensure -> False, boot pool
-> (0, False) (callers treat have < SW_BINDER_COUNT as paused),
prepare -> the gated restore call (which fails closed the same way).
Valid paths are byte-identical (default 64, count=3, string "64",
boundary 256).

## Files changed (in the repo, deployed from HEAD via apply-all)

- `wing/agent.py` — the gate only. No manager, dashboard, or
  wire-protocol change.
- Deliberately NO agent VERSION bump: the manager's agent gate is
  >= 1.0.2, and bumping would churn 53 test files' VERSION assertions
  for zero behavioral gain. The apply-all MANIFEST hash distinguishes
  the deployed agent.py, VERSION stays 1.0.45.

## Tests

`test_agent_binder_count_sink_gate.py` — 148/148 checks (hermetic,
real functions, stubbed shell): regression proof, coerce matrix
(valid -> int, hostile incl. bool/junk-string/NaN/Inf/fractional/
out-of-bounds -> None), hostile matrices refused with zero shell
commands on both sinks, valid paths unchanged (64/3/256/string "64",
full-pool no-write), prepare fast path + absurd-count gating (one
gated restore call, no set build, no hang), gate placement + sibling
name/create-numerics/resize-numerics/status/delete sink gates kept +
env read kept + deploy wiring, negative proof.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight
t173). No new stage files — `wing/agent.py` is already in the
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
- Wing node instance create + boot recovery still work with normal
  counts (valid paths unchanged); an absurd binder count can no longer
  loop binder-control writes from the agent.
