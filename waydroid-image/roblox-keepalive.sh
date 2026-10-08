#!/bin/sh
# roblox-keepalive.sh v2 — force Roblox to stay in the foreground, with a
# crash-loop circuit breaker.
#
# The old version tapped the launch sequence every 10s forever whenever Roblox
# lost focus. If the client crashes on launch (bad build, OOM, moved launcher
# icon), that meant ~360 doomed launch attempts/hour/instance with zero
# visibility — burning CPU and masking the real problem.
#
# v2 counts launch attempts in a 1-hour sliding window. The state file lives
# in the shared data volume, so the count survives container restarts. Over
# budget (default 12/hour) it writes keepalive.degraded — reason on the first
# line, the same convention the host watchdog uses for its .degraded markers
# — and backs off to one attempt every 5 minutes instead of hammering.
# When Roblox is healthy in the foreground again, the marker and the state
# are cleared; the host watchdog mirrors the marker into the dashboard
# "needs attention" badge and auto-clears it on recovery.
#
# Also new: no taps at all while the Roblox package isn't installed yet
# (first-boot APK install in progress) — those attempts were never counted
# before either, but they needlessly fought the installer.
#
# Launch method is unchanged: input taps (monkey/am do not work in this
# Waydroid image), same coordinates the dashboard "Launch Roblox" button uses.
#
# Test overrides (env, all optional): KA_DIR, KA_MAX_PER_HOUR, KA_BACKOFF_S,
# KA_TICK_S, KA_LAUNCH_SLEEP, KA_MAX_ITERS (bounds the loop for tests).

KA_DIR="${KA_DIR:-/data/media/0/droidfarm}"
STATE="$KA_DIR/keepalive.state"
DEGRADED="$KA_DIR/keepalive.degraded"
MAX_PER_HOUR="${KA_MAX_PER_HOUR:-12}"
BACKOFF_S="${KA_BACKOFF_S:-300}"
TICK_S="${KA_TICK_S:-10}"
LAUNCH_SLEEP="${KA_LAUNCH_SLEEP:-1}"
MAX_ITERS="${KA_MAX_ITERS:-0}"

mkdir -p "$KA_DIR" 2>/dev/null

roblox_focused() {
    dumpsys window 2>/dev/null | grep -m1 "mCurrentFocus" | grep -q "com.roblox.client"
}

roblox_installed() {
    pm path com.roblox.client >/dev/null 2>&1
}

# note_attempt: record one launch attempt, print the attempt count inside the
# current 1-hour window. Corrupt/empty state is treated as a fresh window.
note_attempt() {
    now=$(date +%s)
    ws=0
    n=0
    if [ -f "$STATE" ]; then
        # shellcheck disable=SC2162
        read ws n < "$STATE" 2>/dev/null
    fi
    case "$ws$n" in ''|*[!0-9]*) ws=0; n=0;; esac
    if [ "$ws" -eq 0 ] || [ $((now - ws)) -gt 3600 ]; then
        ws=$now
        n=0
    fi
    n=$((n + 1))
    printf '%s %s\n' "$ws" "$n" > "$STATE" 2>/dev/null
    printf '%s' "$n"
}

launch_roblox() {
    # HOME -> app drawer -> tap the Roblox icon (1024x608 coordinates).
    input keyevent 3
    sleep "$LAUNCH_SLEEP"
    input swipe 512 400 512 100
    sleep $((LAUNCH_SLEEP * 2))
    input tap 129 424
}

clear_degraded() {
    rm -f "$DEGRADED" "$STATE" 2>/dev/null
}

mark_degraded() {
    # $1 = attempts in the last hour
    mins=$((BACKOFF_S / 60))
    [ "$mins" -lt 1 ] && mins=1
    printf 'Roblox launch failing (%s attempts in the last hour) - auto-launch backed off, retrying every %s min\n' \
        "$1" "$mins" > "$DEGRADED" 2>/dev/null
}

iters=0
while true; do
    iters=$((iters + 1))
    if [ "$MAX_ITERS" -gt 0 ] && [ "$iters" -gt "$MAX_ITERS" ]; then
        break
    fi
    if roblox_focused; then
        clear_degraded
        sleep "$TICK_S"
        continue
    fi
    if ! roblox_installed; then
        # Client not installed yet (first-boot install in progress): don't
        # hammer the launcher and don't count phantom attempts.
        sleep "$TICK_S"
        continue
    fi
    n=$(note_attempt)
    if [ "$n" -gt "$MAX_PER_HOUR" ]; then
        mark_degraded "$n"
        sleep "$BACKOFF_S"
        continue
    fi
    launch_roblox
    sleep "$TICK_S"
done
