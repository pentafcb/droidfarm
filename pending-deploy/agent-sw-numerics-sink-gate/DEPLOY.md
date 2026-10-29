# agent-sw-numerics-sink-gate — deploy notes

## What it is

The remaining sw_* numerics the t173 session named as milder after
closing the binder pool counts (t170 closed the agent NAME sinks,
t171–t172 the create/resize numerics sinks):

- `sw_probe_agent_http(port, ...)` coerced its port with `int()` but
  never range-checked it — 0/99999/True sailed into a URL build + probe
  attempt — and a bad timeout went straight to `urlopen`.
- `sw_trim_container_logs()` used `trim_max`/`trim_keep` raw. The trim
  half was NOT mild: with a negative `trim_keep` the old code did
  `seek(-trim_keep, SEEK_END)` = `seek(+N, SEEK_END)` — past EOF — so
  `read()` returned `b""` and the trim rewrote the docker json log to
  ZERO bytes. A negative `trim_max` made every log "over threshold" on
  every pass, and a `trim_keep` larger than the file made `seek()` land
  before the file start and WARN on every pass forever.

The failure mode is log destruction / config-driven trim storms and
unvalidated probe attempts — not injection (ports/timeouts/byte counts
never reach a shell).

New shared-coercion helpers in `wing/agent.py` bound the port to
[1, 65535], the timeout to a positive finite float, and both byte
counts to positive ints (no upper bound — docker json logs legitimately
run to tens of GB; the defaults are 1GB). Invalid values fail closed
before any touch: probe -> `("bad", False)` with zero `urlopen` calls;
trim -> return with one WARN per day (event-window deduped via
`log.trim.badconfig.ev`, never silent, never spammy). In the trim loop,
`trim_keep >= size` is now a silent no-op skip (keeping the whole file
is not a trim). Valid paths are byte-identical (default 1GB/1GB,
overrides, probe 200/500/truncated/empty).

## Files changed (in the repo, deployed from HEAD via apply-all)

- `wing/agent.py` — the gates only. No manager, dashboard, or
  wire-protocol change.
- Deliberately NO agent VERSION bump: the manager's agent gate is
  >= 1.0.2, and bumping would churn 53 test files' VERSION assertions
  for zero behavioral gain. The apply-all MANIFEST hash distinguishes
  the deployed agent.py, VERSION stays 1.0.45.

## Tests

`test_agent_sw_numerics_sink_gate.py` — 96/96 checks (hermetic, real
functions, stubbed urlopen, real temp files): regression proof
(old-logic simulation wipes a log; old `int()` accepts 0/True/99999),
coerce matrices, hostile ports/timeouts with zero urlopen calls,
valid probe paths via stubbed urlopen (ok/bad/truncated/empty, string/
float/boundary ports, URL carries the coerced port), hostile trim
params with untouched files + one WARN per day + event file, valid
trim paths unchanged (trim to keep, tail bytes, same inode,
under-threshold untouched, logged, event noted, overrides),
keep>=size skip, gate placement + sibling gates kept + deploy wiring,
negative proof.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight
t174). No new stage files — `wing/agent.py` is already in the
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
- Wing node selfwatch still trims over-threshold docker logs and still
  probes its own HTTP server (valid paths unchanged); a bad
  `DF_LOG_TRIM_MAX`/`DF_LOG_TRIM_KEEP` value can no longer wipe logs,
  and an out-of-range probe port fails closed without a network attempt.
