# df-watchdog v13 — bounded docker calls

## Problem
Every `docker` invocation in `df-watchdog.sh` (24 of them: `ps`, `inspect`,
`start`/`restart`, `exec` probes, prunes) was unbounded — no `timeout`
anywhere. A hung docker (wedged daemon, or a wedged waydroid session making
`docker exec` hang) froze the whole minute-cron run forever. The v11 lock
only serialized runs: each new holder hung on the same wedged call, so NO
check ever ran — including the manager self-check (v5/v12), the last-resort
auto-recovery. The watchdog designed to recover from a wedged box was itself
killed by a wedged box. (The manager and the wing agent already bound every
subprocess call; the shell watchdog was the one surface left.)

## Fix (waydroid-image/df-watchdog.sh, v13)
- New `dw()` wrapper: `dw <secs> <docker args...>` runs docker under
  `timeout`; exit 124 on timeout. Falls back to bare docker when the
  `timeout` binary is missing (never break the watchdog over it).
- 11 call sites converted: `ps`, `inspect`, `start`, `restart`, session
  probe, `exec -d session start`, helper `pgrep`, `pkill`, dumpsys version
  check, image + builder prune. Timeouts are env-overridable for tests:
  `DF_DW_FAST` (10s, cheap probes), `DF_DW_ACTION` (30s, start/restart),
  `DF_DW_SLOW` (120s, prunes).
- A timeout is UNKNOWN, not dead: the session probe and helper `pgrep`
  probes skip recovery that cycle (one WARN per instance per day via
  `_probe_timeout_warn`) instead of issuing restarts against a
  possibly-live box or burning circuit-breaker budgets. A blind cycle also
  blocks the section-6 DEGRADED auto-clear (healthy=0) — recovery is never
  declared while the probes are blind.
- Everywhere else a timeout behaves like any other docker failure:
  strikes, FAILED logs, circuit budgets — the machinery built for exactly
  this. The run now always completes, the v11 lock always releases, and the
  next minute retries.
- The two backgrounded helper restarts stay bare `docker` on purpose: the
  exec client stays attached to the looping helper for its whole life, and
  a timeout there would kill the helper it just started. Liveness is
  re-verified by the bounded pgrep probe next minute.

## Tests
`test_watchdog_timeout.sh` — 50/50 hermetic (fake docker with per-pattern
hang modes, `exec sleep` so `timeout`'s TERM kills it directly):
bounded completion of hung session/helper/ps/prune/dumpsys/session-start
probes, unknown-skip semantics (no false session starts or helper
restarts, no budget burn, WARN deduped to one/day), fast-failure still
takes the dead path (regressions), no-timeout-binary fallback branch,
DEGRADED kept while blind and cleared on recovery, source contracts
(11 dw sites, 2 intentional bare-docker restarts).

Regressions: watchdog v4 26/26, v5 27/27, v6 40/40, v9 34/34 (harness now
extracts the v13 block; S11 contract updated to the dw-bounded idiom),
keepalive-v2 38/38, lock 22/22, v12 34/34.
