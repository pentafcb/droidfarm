# wing-selfwatch-lock

Agent 1.0.23: single-instance lock for the wing selfwatch loop — the
thirteenth "main had it, wing didn't" parity item (the main-node watchdog
got the `mkdir` mutex in v11 on 2026-10-11).

Agent 1.0.25: pidfile-grace race fix (see below).

## The gap
The agent binds 8090–8099 with a port-conflict fallback. A stale or duplicate
agent process on the same node (crashed installer re-run, manual start, the
`__main__` restart loop racing systemd) binds the next free port and both
processes run the full selfwatch recovery loop — duplicate container starts,
double helper restarts, double disk prunes — while heartbeating the same
provider ID on different ports.

## The fix
- `sw_selfwatch_lock()`: `mkdir` mutex at `<SELF_WATCH_DIR>/selfwatch.lock`
  with the holder's pid; mtime refreshed every 60s pass; locks older than
  180s or with a dead holder pid are taken over; atomic `mkdir` means two
  racers always resolve to one holder. Returns held/foreign/broken, never
  raises.
- `selfwatch_pass_once()`: recovery actions (per-instance checks + host
  guards) run only when this process holds the lock; the per-process HTTP
  self-check (1.0.17) always runs. `selfwatch_loop()` delegates to it.
- Visibility: one WARN for a foreign holder (then silent), "recovery
  resumed" on takeover, one WARN/day for a broken lock dir — all to
  `selfwatch.log`, surfaced by the dashboard Eventos modal with zero
  manager/dashboard changes.

## Tests
`test_wing_selfwatch_lock.py` — 51/51 hermetic: version + gate, pid-alive
units, lock state machine (fresh/re-entrant/foreign/dead/stale/missing
pidfile/broken/garbage/file-not-dir), once-only notes, pass gating
(held/foreign/broken/raising/garbage lock_fn), loop wiring contract,
thread-churn smoke, pidfile-grace race regression (slow-winner held,
dead-winner takeover, grace bounded).

## 1.0.25: pidfile-grace race fix
The mkdir winner writes its pidfile in the syscalls immediately after
`mkdir`. A loser arriving in that window read a missing pidfile, concluded
the holder was dead, and `rmdir()`'d the winner's lock dir — the winner's
pidfile write then failed and it falsely reported "broken" (a false
"lock unusable" signal + WARN). The suite's own 10-thread churn test caught
it as an intermittent 43/44. New `SW_LOCK_PIDFILE_GRACE_S` (0.5s): a missing
pidfile waits a beat before the lock is treated as abandoned; a crashed
winner never writes, so the dead-holder takeover still recovers it. No
behavior change for the fresh/foreign/stale paths.
