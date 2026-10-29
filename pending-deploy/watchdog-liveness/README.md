# watchdog-liveness

Proves the minute-cron df-watchdog is actually firing, not just installed.

**Problem.** The morning's final verify gate (`verify-live.sh --ssh`) checked
that `df-watchdog.sh` is deployed (sha) and that the cron daemon is active —
but the daemon can be up while the watchdog cron entry is missing, broken, or
silently not executing. A healthy no-op watchdog run logs nothing, so the log
mtime can't distinguish "firing quietly" from "never ran". The v11 lock dir is
released at exit, so it can't either. The entire auto-recovery story could be
dead with a green verify.

**Fix.** `df-watchdog.sh` touches `$ST/heartbeat` (default
`/tmp/df-watchdog/heartbeat`) on every executed run, placed after the v11
lock acquisition so a run that exits silently on a foreign lock never fakes
liveness. `verify-live.sh --ssh` reports the heartbeat age, computed on the
server (`$(date +%s)` inside the remote heredoc — no client clock skew):
- missing -> WARN "the minute cron has never fired (or the box predates the
  liveness touch; rerun baseline-newbox.sh)"
- >300s -> WARN "not executing — check /etc/cron.d/jerusalem-watchdog"
- else OK "df-watchdog firing (heartbeat Ns ago)"

WARN-only: an operational flag for the SSH session, not a code-currency
check (same as the cron-daemon probe). Wing nodes need no change — their
selfwatch liveness is proven by agent heartbeats, which the manager already
tracks (recently-heartbeated filter in the node-events fetch).

**Tests.** `test_watchdog_liveness.sh` runs the real watchdog against fake
docker/curl/systemctl (11/11): syntax, fresh touch, mtime refresh, foreign
lock -> untouched + no docker calls, empty regular file, broken lock dir ->
WARN + untouched, DF_ST honored, healthy-run no-op regression.
`test_live_verify.sh` gains T39/T40/T41 (probe mapping) + T42 (server-side
age regression guard) — 119/119. Wired as preflight t113; t32 count 112 -> 119.
