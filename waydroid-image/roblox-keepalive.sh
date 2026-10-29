#!/bin/sh
# roblox-keepalive.sh v3 — force Roblox to stay in the foreground, with a
# crash-loop circuit breaker AND bounded probes.
#
# v2 (kept): counts launch attempts in a 1-hour sliding window (state in the
# shared data volume, survives container restarts). Over budget (default
# 12/hour) it writes keepalive.degraded — reason on the first line, the same
# convention the host watchdog uses for its .degraded markers — and backs off
# to one attempt every 5 minutes instead of hammering. When Roblox is healthy
# in the foreground again, the marker and the state are cleared; the host
# watchdog (v8 check 3b) and the wing agent (1.0.15 check 3b) mirror the
# marker into the dashboard "needs attention" badge and auto-clear it on
# recovery.
#
# v3 (new): every Android probe runs under `timeout`. A wedged Android
# session makes `dumpsys window` / `pm path` hang forever — the old loop
# froze inside roblox_focused() with pgrep still seeing the script alive
# (so no watchdog ever restarted it), keepalive.degraded was never written
# (so the dashboard never showed it), and Roblox sat unfocused indefinitely
# with a green dashboard. Now a timed-out probe is UNKNOWN, not "not
# focused": the loop skips the launch this cycle (tapping at a wedged
# session is pointless and must not burn the launch-attempt budget), and
# after KA_PROBE_FAILS consecutive FOCUS-probe timeouts it writes
# keepalive.degraded with a probe-timeout reason — surfacing through the
# existing v8/1.0.15 mirror with zero manager/dashboard changes. A
# successful focus probe resets the counter; Roblox focused again clears
# the marker (existing lifecycle). A timed-out `pm path` probe (package
# manager wedged while the window manager answers) skips the cycle
# conservatively without touching the counter — only the focus probe
# determines whether we can verify Roblox focus.
# The launch `input` commands are bounded the same way (cheap insurance for
# a session that wedges between the probe and the launch).
#
# Also kept from v2: no taps at all while the Roblox package isn't installed
# yet (first-boot APK install in progress).
#
# Launch method is unchanged: input taps (monkey/am do not work in this
# Waydroid image), same coordinates the dashboard "Launch Roblox" button uses.
#
# `timeout` is toybox here (present in this image — screencap-loop.sh
# already relies on `timeout 5 screencap`); when it is missing we fall back
# to the old unbounded calls rather than break the loop over it.
#
# Test overrides (env, all optional): KA_DIR, KA_MAX_PER_HOUR, KA_BACKOFF_S,
# KA_TICK_S, KA_LAUNCH_SLEEP, KA_MAX_ITERS (bounds the loop for tests),
# KA_PROBE_TIMEOUT_S (seconds per probe, default 10),
# KA_PROBE_FAILS (consecutive timeouts before the degraded marker, default 3).

KA_DIR="${KA_DIR:-/data/media/0/jerusalem}"
STATE="$KA_DIR/keepalive.state"
DEGRADED="$KA_DIR/keepalive.degraded"
MAX_PER_HOUR="${KA_MAX_PER_HOUR:-12}"
BACKOFF_S="${KA_BACKOFF_S:-300}"
TICK_S="${KA_TICK_S:-10}"
LAUNCH_SLEEP="${KA_LAUNCH_SLEEP:-1}"
MAX_ITERS="${KA_MAX_ITERS:-0}"
KA_PROBE_TIMEOUT_S="${KA_PROBE_TIMEOUT_S:-10}"
KA_PROBE_FAILS="${KA_PROBE_FAILS:-3}"
[ "$KA_PROBE_TIMEOUT_S" -ge 1 ] 2>/dev/null || KA_PROBE_TIMEOUT_S=10
[ "$KA_PROBE_FAILS" -ge 1 ] 2>/dev/null || KA_PROBE_FAILS=3

mkdir -p "$KA_DIR" 2>/dev/null

# aw: run "$@" under `timeout` when available. Returns the command's rc
# (124 on timeout); without the timeout binary this is a plain call.
if command -v timeout >/dev/null 2>&1; then
    HAVE_TIMEOUT=1
else
    HAVE_TIMEOUT=0
fi
aw() {
    if [ "$HAVE_TIMEOUT" = "1" ]; then
        timeout "$KA_PROBE_TIMEOUT_S" "$@"
    else
        "$@"
    fi
}

roblox_focused() {
    # rc 0 = Roblox focused, 1 = not focused, 2 = probe timed out (unknown).
    # A timeout is NOT "not focused" — callers must skip the launch cycle.
    out=$(aw dumpsys window 2>/dev/null); rc=$?
    if [ "$rc" -eq 124 ]; then
        return 2
    fi
    if [ "$rc" -ne 0 ]; then
        return 1
    fi
    printf '%s' "$out" | grep -m1 "mCurrentFocus" | grep -q "com.roblox.client"
}

roblox_installed() {
    # rc 0 = installed, 1 = not installed, 2 = probe timed out (unknown).
    aw pm path com.roblox.client >/dev/null 2>&1; rc=$?
    if [ "$rc" -eq 124 ]; then
        return 2
    fi
    return "$rc"
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
    # Bounded: a session that wedges between the probe and the launch must
    # not freeze the loop inside `input`.
    aw input keyevent 3
    sleep "$LAUNCH_SLEEP"
    aw input swipe 512 400 512 100
    sleep $((LAUNCH_SLEEP * 2))
    aw input tap 129 424
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

mark_probe_degraded() {
    # $1 = consecutive timed-out probes. Reason on the first line: the host
    # watchdog (v8 check 3b) and the wing agent (1.0.15 check 3b) mirror the
    # first 160 chars into the dashboard "needs attention" badge.
    printf 'Android session unresponsive (%s consecutive probe timeouts) - keepalive cannot verify Roblox focus; not launching\n' \
        "$1" > "$DEGRADED" 2>/dev/null
}

probe_fails=0
iters=0
while true; do
    iters=$((iters + 1))
    if [ "$MAX_ITERS" -gt 0 ] && [ "$iters" -gt "$MAX_ITERS" ]; then
        break
    fi
    roblox_focused; frc=$?
    if [ "$frc" -eq 0 ]; then
        probe_fails=0
        clear_degraded
        sleep "$TICK_S"
        continue
    fi
    if [ "$frc" -eq 2 ]; then
        # Probe timed out: UNKNOWN, not "not focused". Skip the launch —
        # tapping at a wedged session is pointless and must not burn the
        # launch-attempt budget.
        probe_fails=$((probe_fails + 1))
        if [ "$probe_fails" -ge "$KA_PROBE_FAILS" ]; then
            mark_probe_degraded "$probe_fails"
        fi
        sleep "$TICK_S"
        continue
    fi
    probe_fails=0
    roblox_installed; irc=$?
    if [ "$irc" -eq 2 ]; then
        # pm probe timed out while dumpsys answered (a wedged package
        # manager with a responsive window manager). We cannot confirm the
        # client is installed, so skip the launch conservatively — but this
        # does not count toward the focus-probe degraded threshold: only
        # the focus probe determines whether we can verify Roblox focus.
        sleep "$TICK_S"
        continue
    fi
    if [ "$irc" -ne 0 ]; then
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
