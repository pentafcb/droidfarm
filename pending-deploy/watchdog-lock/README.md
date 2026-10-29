# df-watchdog v11 — single-instance lock

## Problem
`df-watchdog.sh` runs every minute via cron. If `docker` hangs (wedged
daemon — the exact failure the v5 manager self-check guards against at the
service level), every cron run spawns another watchdog, each spawning more
`docker` subprocesses, stacking forever with nothing to reap them. The
watchdog — the last-resort auto-recovery mechanism — could pile up into a
fork-bomb-shaped load spike on an already-wedged box.

## Fix (waydroid-image/df-watchdog.sh, v11)
- `mkdir` mutex at `/tmp/df-watchdog.lock` (same atomic pattern as the
  Windows installer), acquired before any `docker` call.
- Second run while one holds the lock exits silently (exit 0).
- Lock older than `DF_LOCK_STALE_SECS` (default 600s; a healthy run takes
  well under a minute) is treated as stale — previous run crashed or was
  SIGKILLed — and taken over with exactly one `WARN watchdog: stale lock`
  log line.
- The `EXIT` trap releases only OUR lock (pid file checked), so a hung run
  that lost a stale-takeover race can never delete the new holder's lock.
- Broken lock dir (cannot `mkdir`) → one `WARN watchdog: cannot create
  lock dir` + skip the run, so a broken `/tmp` can't silently disable the
  watchdog forever. Fresh lock held by another run → silent exit.
- Backgrounded per-instance checks clear the inherited `EXIT` trap
  (belt-and-braces; verified `&` subshells don't run it on this bash).
- Test overrides: `DF_LOCKDIR`, `DF_LOCK_STALE_SECS` (documented in the
  header comment block).

Nothing else changes: same checks, same circuit breakers, same log path.
Never touches instance data, credentials, or tokens.

## Tests
`test_watchdog_lock.sh` — 22/22 hermetic (real watchdog vs fake
docker/df/curl/systemctl): normal acquire/run/release, concurrent silent
exit, stale takeover (one WARN), broken lock dir (WARN + skip), lock held
with run pid mid-run, pid-checked release on SIGTERM after a simulated
takeover. The suite also proves (by temporarily reverting) that its T5
scenario would catch a premature lock release.

Existing watchdog suites (v4/v5/v6/keepalive-v2) now pin
`DF_LOCKDIR` to their own temp dir so the full preflight stays hermetic.
