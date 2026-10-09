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
# markers and never restarts anything — pure surfacing.
#
# Cost: one `waydroid shell` probe per interval ONLY while the marker is
# missing (steady state = two file stats per interval, ~nothing).
#
# Test seams (env only, no-ops in prod):
#   JERUSALEM_DATA_DIR       data dir (default /root/.local/share/waydroid/data/media/0/jerusalem)
#   BOOTED_WATCHER_INTERVAL  seconds between checks (default 60)
#   BOOTED_WATCHER_MAX_ITERS bound the loop for tests (default 0 = forever)
S="${JERUSALEM_DATA_DIR:-/root/.local/share/waydroid/data/media/0/jerusalem}"
INTERVAL="${BOOTED_WATCHER_INTERVAL:-60}"
MAX_ITERS="${BOOTED_WATCHER_MAX_ITERS:-0}"
PKG="com.roblox.client"
n=0
while true; do
  if [ ! -f "$S/booted" ]; then
    if waydroid shell getprop sys.boot_completed 2>/dev/null | grep -q '^1$'; then
      mkdir -p "$S" 2>/dev/null
      if touch "$S/booted" 2>/dev/null; then
        echo "[booted-watcher] Android boot completed (late) - marker written"
      fi
    fi
  fi
  if [ -f "$S/booted" ] && [ ! -f "$S/roblox_done" ]; then
    if waydroid shell pm list packages "$PKG" 2>/dev/null | grep -q "$PKG"; then
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
