# wing-selfwatch-lock — single-instance lock for the wing selfwatch loop

## Problem

The agent binds 8090–8099 with a port-conflict fallback (`main()` tries each
port in turn on `OSError`). If a stale or duplicate agent process is alive on
the same node — a crashed installer re-run, a manual start, the `__main__`
restart loop racing systemd — the second process binds the next free port and
**both** processes run the full selfwatch recovery loop: duplicate container
starts, double helper restarts, double disk prunes, double log trims, while
heartbeating the same provider ID on different ports (the manager's tracked
node port flaps). The per-process circuit-breaker budgets live partly in
shared event files but the recovery *actions* never coordinated.

The main-node `df-watchdog.sh` v11 fixed the same class (stacked watchdogs on
a hung docker → fork-bomb load) with a `mkdir` mutex. This is the thirteenth
"main had it, wing didn't" parity item (1.0.5 drift, 1.0.6
adaptive-screencap, 1.0.7 disk/log, 1.0.11 session-death, 1.0.12 loop
devices, 1.0.13 memory, 1.0.14 stuck-boot, 1.0.15 keepalive mirror, 1.0.16
binderfs self-heal, 1.0.17 agent HTTP self-check, 1.0.18 create image
preflight, 1.0.19 auth rate-limit, 1.0.20 status cache, 1.0.21 headroom probe,
1.0.22 per-instance degraded).

## What changed

- `wing/agent.py` **1.0.22 → 1.0.23**: new single-instance lock section.
  - `sw_selfwatch_lock(st_dir=None)` → `("held" | "foreign" | "broken",
    detail)`: a `mkdir` mutex at `<SELF_WATCH_DIR>/selfwatch.lock` holding
    the holder's pid. The holder refreshes the lock-dir mtime every pass
    (60s); a lock older than `SW_LOCK_STALE_SECS` (180s) or with a dead
    holder pid is taken over. `mkdir` is atomic, so two racers always
    resolve to exactly one holder. Never raises.
  - `selfwatch_pass_once(lock_fn=None)` (new test seam): the recovery
    actions (per-instance checks + the four host guards) run only when this
    process holds the lock; the per-process agent HTTP self-check (1.0.17)
    always runs — a wedged listener must still exit its own process so
    systemd can revive it. `selfwatch_loop()` now just calls it in a
    `while True` + sleep; the supervisor registration is unchanged.
  - Notes: the "foreign" process logs one WARN then stays silent; a
    takeover logs "recovery resumed" and re-arms the foreign note; a
    broken lock dir fails safe to skip + one WARN per day (v11 semantics).
    All notes go to `selfwatch.log`, so the dashboard Eventos modal
    surfaces them with zero manager/dashboard changes.
- Manager: **no change** (`>=1.0.2` gates accept 1.0.23). Dashboard: **no
  change**.

## Deploy

Rides `apply-all` from HEAD — `wing/agent.py` is already in the stage list
(no new stage files). After deploy, the vultr node needs Penta's installer
re-run for agent 1.0.23 (until then srt nodes run the old lock-free
selfwatch — today's behavior, nothing misreports).

## Verify

- `python3 pending-deploy/wing-selfwatch-lock/test_wing_selfwatch_lock.py`
  → `44 passed, 0 failed`
- Full `deploy-all.sh --stage-only` dry run green (preflight t80,
  queue-audit 0 findings)

## Tests

`test_wing_selfwatch_lock.py` (hermetic): VERSION exact 1.0.23 + manager
`>=1.0.2` gate, `_sw_pid_alive` units (own/dead/garbage), lock units
(fresh/re-entrant/foreign-live/dead-holder/stale-holder/missing-pidfile/
broken-dir/garbage-dir/lock-path-is-a-file), once-only note helpers
(foreign once, takeover re-arms, broken once-per-day), `selfwatch_pass_once`
gating (recovery only when held, HTTP self-check always runs, raising or
garbage `lock_fn` never propagates), `selfwatch_loop` wiring contract
(delegates to `selfwatch_pass_once`, no inline guard calls, supervisor still
registers `selfwatch`), 10-thread × 20-call churn smoke.
