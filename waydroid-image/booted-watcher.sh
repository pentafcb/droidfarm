#!/bin/sh
# booted-watcher.sh — persistent booted-marker writer for Waydroid instances.
#
# PROBLEM IT FIXES: entrypoint.sh waits at most 5 minutes for
# sys.boot_completed; under load (fleet recovery, many instances booting at
# once) Android can take longer. The one-shot window then closes with no
# `booted` marker ever written — even though Android finishes booting minutes
# later. Consequences observed in the 2026-10-09 fleet recovery: the
# dashboard shows "booting" forever for healthy instances, and the host
# watchdog's stuck-boot check restarts a HEALTHY container at 15 minutes
# (restart loop -> circuit breaker DEGRADED -> auto-recovery pauses).
#
# This loop runs forever in the container (started by entrypoint.sh after the
# one-shot boot wait). Whenever sys.boot_completed=1 is observed and the
# marker is missing, it writes it (idempotent). It also backfills roblox_done
# when the client is present but that marker is missing. It NEVER deletes
# markers and never restarts anything — pure surfacing, EXCEPT for the
# late-boot hook below: when the entrypoint's 5-min window missed the boot,
# the whole boot-completion sequence (Roblox install, helper scripts,
# helpers, optimizer, launch) never ran, so the watcher runs it now via
# /opt/boot-complete.sh (same script the entrypoint's fast path uses).
# Old images without /opt/boot-complete.sh keep the pure-surfacing behavior.
#
# Cost: one `waydroid shell` probe per interval ONLY while the marker is
# missing (steady state = two file stats per interval, ~nothing).
#
# Test seams (env only, no-ops in prod):
#   JERUSALEM_DATA_DIR       data dir (default /root/.local/share/waydroid/data/media/0/jerusalem)
#   BOOTED_WATCHER_INTERVAL  seconds between checks (default 60)
#   BOOTED_WATCHER_MAX_ITERS bound the loop for tests (default 0 = forever)
#   BOOT_COMPLETE_SH         boot-complete script (default /opt/boot-complete.sh)
#   BOOTED_WATCHER_PROBE_TIMEOUT_S seconds per `waydroid shell` probe
#                            (default 30). A wedged Waydroid session hangs
#                            `waydroid shell` forever; without the bound the
#                            watcher froze and the late-boot marker was never
#                            written (dashboard stuck on "booting" until the
#                            watchdog's 15-min stuck-boot restart). A timed-out
#                            probe is UNKNOWN, not "not booted": the cycle is
#                            skipped, never acted on.
S="${JERUSALEM_DATA_DIR:-/root/.local/share/waydroid/data/media/0/jerusalem}"
INTERVAL="${BOOTED_WATCHER_INTERVAL:-60}"
MAX_ITERS="${BOOTED_WATCHER_MAX_ITERS:-0}"
BOOT_COMPLETE_SH="${BOOT_COMPLETE_SH:-/opt/boot-complete.sh}"
BOOTED_WATCHER_PROBE_TIMEOUT_S="${BOOTED_WATCHER_PROBE_TIMEOUT_S:-30}"
[ "$BOOTED_WATCHER_PROBE_TIMEOUT_S" -ge 1 ] 2>/dev/null || BOOTED_WATCHER_PROBE_TIMEOUT_S=30
PKG="com.roblox.client"

# bw: run "$@" under `timeout` when available (same pattern as
# roblox-keepalive.sh's aw()). Returns the command's rc (124 on timeout);
# without the timeout binary this is a plain call. Callers treat 124 as
# "probe hung, skip this cycle" — never as a negative answer.
if command -v timeout >/dev/null 2>&1; then
    HAVE_TIMEOUT=1
else
    HAVE_TIMEOUT=0
fi
bw() {
    if [ "$HAVE_TIMEOUT" = "1" ]; then
        timeout "$BOOTED_WATCHER_PROBE_TIMEOUT_S" "$@"
    else
        "$@"
    fi
}
n=0
while true; do
  if [ ! -f "$S/booted" ]; then
    boot_out=$(bw waydroid shell getprop sys.boot_completed 2>/dev/null)
    bw_rc=$?
    if [ "$bw_rc" = "124" ]; then
      echo "[booted-watcher] WARN: boot probe timed out after ${BOOTED_WATCHER_PROBE_TIMEOUT_S}s - skipping cycle"
    elif printf '%s\n' "$boot_out" | grep -q '^1$'; then
      mkdir -p "$S" 2>/dev/null
      # Late boot: the entrypoint's fast path never ran, so run the full
      # boot-completion sequence now (it writes the booted marker itself).
      # Idempotent — a retry next interval is safe if it fails partway.
      if [ -f "$BOOT_COMPLETE_SH" ]; then
        echo "[booted-watcher] late boot observed - running boot-complete.sh"
        sh "$BOOT_COMPLETE_SH" >/dev/null 2>&1 || \
          echo "[booted-watcher] WARN: boot-complete.sh exited non-zero"
      fi
      if touch "$S/booted" 2>/dev/null; then
        echo "[booted-watcher] Android boot completed (late) - marker written"
      fi
    fi
  fi
  if [ -f "$S/booted" ] && [ ! -f "$S/roblox_done" ]; then
    pm_out=$(bw waydroid shell pm list packages "$PKG" 2>/dev/null)
    bw_rc=$?
    if [ "$bw_rc" = "124" ]; then
      echo "[booted-watcher] WARN: package probe timed out after ${BOOTED_WATCHER_PROBE_TIMEOUT_S}s - skipping cycle"
    elif printf '%s\n' "$pm_out" | grep -q "$PKG"; then
      if touch "$S/roblox_done" 2>/dev/null; then
        echo "[booted-watcher] Roblox client present - roblox_done written"
      fi
    fi
  fi
  n=$((n + 1))
  if [ "$MAX_ITERS" -gt 0 ] && [ "$n" -ge "$MAX_ITERS" ]; then
    break
  fi
  sleep "$INTERVAL"
done
