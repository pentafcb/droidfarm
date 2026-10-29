#!/bin/bash
# test_keepalive_v2.sh — unit tests for roblox-keepalive.sh v2 (crash-loop
# circuit breaker) and the df-watchdog.sh v8 mirroring of keepalive.degraded.
# Runs the real scripts against fake dumpsys/input/pm/docker. Hermetic.
# Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
KA_SRC="$HERE/../../waydroid-image/roblox-keepalive.sh"
WD_SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export T

echo "== bash -n =="
bash -n "$KA_SRC" && ok "keepalive syntax" || bad "keepalive syntax"
bash -n "$WD_SRC" && ok "watchdog syntax" || bad "watchdog syntax"

# ---------- fake Android tools ----------
BIN="$T/fakebin"; mkdir -p "$BIN"
cat > "$BIN/dumpsys" <<'EOF'
#!/bin/sh
# prints mCurrentFocus for the scenario in $T/focus; FAIL -> exit 1
v=$(cat "$T/focus" 2>/dev/null) || exit 1
[ "$v" = "FAIL" ] && exit 1
echo "mCurrentFocus=Window{abcd u0 $v}"
EOF
cat > "$BIN/input" <<'EOF'
#!/bin/sh
echo "$*" >> "$T/input.log"
EOF
cat > "$BIN/pm" <<'EOF'
#!/bin/sh
# $T/pm_code: 0 = installed, else missing
[ "$(cat "$T/pm_code" 2>/dev/null || echo 0)" = "0" ] && { echo "package:/data/app/com.roblox.client.apk"; exit 0; }
exit 1
EOF
chmod +x "$BIN"/*

ka_run() { # ka_run <iters> [budget]: run the keepalive bounded
    local iters=$1 budget=${2:-3}
    KA_DIR="$T/df" KA_TICK_S=0 KA_LAUNCH_SLEEP=0 KA_MAX_ITERS="$iters" \
      KA_MAX_PER_HOUR="$budget" KA_BACKOFF_S=0 \
      PATH="$BIN:/usr/bin:/bin" sh "$KA_SRC" >/dev/null 2>&1
}
ka_setup() { # fresh scenario
    rm -rf "$T/df"; mkdir -p "$T/df"
    : > "$T/input.log"
    echo "0" > "$T/pm_code"
}
input_count() { grep -c "^$1$" "$T/input.log" 2>/dev/null; }

FOCUS_RBX="com.roblox.client/com.roblox.client.MainActivity"
FOCUS_HOME="com.android.launcher3/com.android.launcher3.Launcher"

echo "== K1: Roblox focused -> no taps, no state, no marker =="
ka_setup; echo "$FOCUS_RBX" > "$T/focus"
ka_run 3
[ "$(input_count 'tap 129 424')" = 0 ] && ok "K1 no taps" || bad "K1 tapped while focused"
[ ! -f "$T/df/keepalive.state" ] && ok "K1 no state file" || bad "K1 state file written"
[ ! -f "$T/df/keepalive.degraded" ] && ok "K1 no degraded marker" || bad "K1 degraded marker written"

echo "== K2: crash loop -> 3 launches then backoff + marker =="
ka_setup; echo "$FOCUS_HOME" > "$T/focus"
ka_run 5 3
[ "$(input_count 'tap 129 424')" = 3 ] && ok "K2 exactly 3 launch taps" || bad "K2 taps=$(input_count 'tap 129 424') (want 3)"
[ "$(input_count 'keyevent 3')" = 3 ] && ok "K2 3 HOME keyevents" || bad "K2 HOME count wrong"
[ "$(input_count 'swipe 512 400 512 100')" = 3 ] && ok "K2 3 drawer swipes" || bad "K2 swipe count wrong"
[ "$(cat "$T/df/keepalive.state" 2>/dev/null | awk '{print $2}')" = 5 ] && ok "K2 state counts all attempts (5)" || bad "K2 state wrong: $(cat "$T/df/keepalive.state" 2>/dev/null)"
[ -f "$T/df/keepalive.degraded" ] && ok "K2 degraded marker written" || bad "K2 no degraded marker"
grep -q "attempts in the last hour" "$T/df/keepalive.degraded" && ok "K2 reason mentions attempts" || bad "K2 reason text wrong"
grep -q "backed off" "$T/df/keepalive.degraded" && ok "K2 reason mentions backoff" || bad "K2 reason missing backoff"
[ "$(head -c 160 "$T/df/keepalive.degraded" | wc -c)" -lt 160 ] && ok "K2 reason fits 160-char watchdog mirror" || bad "K2 reason too long"

echo "== K3: recovery clears marker + state =="
echo "$FOCUS_RBX" > "$T/focus"   # keep K2's marker + state on disk
ka_run 2 3
[ ! -f "$T/df/keepalive.degraded" ] && ok "K3 marker cleared on recovery" || bad "K3 marker stuck"
[ ! -f "$T/df/keepalive.state" ] && ok "K3 state cleared on recovery" || bad "K3 state stuck"
[ "$(input_count 'tap 129 424')" = 3 ] && ok "K3 no new taps after recovery" || bad "K3 tapped after recovery"

echo "== K4: package not installed -> no taps, no counting =="
ka_setup; echo "$FOCUS_HOME" > "$T/focus"; echo "1" > "$T/pm_code"
ka_run 3 3
[ "$(input_count 'tap 129 424')" = 0 ] && ok "K4 no taps during install" || bad "K4 tapped during install"
[ ! -f "$T/df/keepalive.state" ] && ok "K4 no state during install" || bad "K4 state written during install"
[ ! -f "$T/df/keepalive.degraded" ] && ok "K4 no marker during install" || bad "K4 marker written during install"

echo "== K5: corrupt state file -> treated as fresh window =="
ka_setup; echo "$FOCUS_HOME" > "$T/focus"
echo "garbage-not-numbers" > "$T/df/keepalive.state"
ka_run 2 3
[ "$(input_count 'tap 129 424')" = 2 ] && ok "K5 2 taps after corrupt state" || bad "K5 taps=$(input_count 'tap 129 424')"
[ "$(cat "$T/df/keepalive.state" | awk '{print $2}')" = 2 ] && ok "K5 window reset to 2" || bad "K5 state: $(cat "$T/df/keepalive.state")"

echo "== K6: expired window resets the count =="
ka_setup; echo "$FOCUS_HOME" > "$T/focus"
echo "$(( $(date +%s) - 4000 )) 3" > "$T/df/keepalive.state"
ka_run 1 3
[ "$(cat "$T/df/keepalive.state" | awk '{print $2}')" = 1 ] && ok "K6 count reset to 1" || bad "K6 state: $(cat "$T/df/keepalive.state")"
[ ! -f "$T/df/keepalive.degraded" ] && ok "K6 no marker after reset" || bad "K6 marker written"

echo "== K7: dumpsys failure -> treated as not-focused (taps) =="
ka_setup; echo "FAIL" > "$T/focus"
ka_run 1 3
[ "$(input_count 'tap 129 424')" = 1 ] && ok "K7 taps on dumpsys failure" || bad "K7 no tap"

echo "== K8: marker not rewritten while healthy path runs =="
ka_setup; echo "$FOCUS_HOME" > "$T/focus"
ka_run 4 3   # 3 launches + 1 backoff tick -> marker written
[ -f "$T/df/keepalive.degraded" ] && ok "K8 marker present" || bad "K8 no marker"
m1=$(md5sum < "$T/df/keepalive.degraded")
ka_run 1 3   # another backoff tick: count 5, marker rewritten with new count
m2=$(md5sum < "$T/df/keepalive.degraded")
[ "$m1" != "$m2" ] && ok "K8 marker updated with new count" || bad "K8 marker stale"

# ---------- watchdog mirroring (fake docker, like test_watchdog_v4.sh) ----------
WBIN="$T/wbin"; WST="$T/wst"; WVOL="$T/wvol"; WLOG="$T/wdog.log"; CALLS="$T/wcalls"
mkdir -p "$WBIN" "$WST" "$WVOL"
export CALLS
cat > "$WBIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
sub=$1; shift
case "$sub" in
  ps)      cat "$T/docker_ps" 2>/dev/null ;;
  inspect) date -u +%FT%TZ ;;
  start|restart) exit 0 ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep) exit 0 ;;   # all helpers alive
      waydroid) # v9 session probe: `waydroid shell getprop sys.boot_completed`
        case "$*" in *getprop*) echo 1; exit 0;; esac
        exit 0 ;;
      *) exit 0 ;;       # dumpsys package -> empty (version-pin skips)
    esac ;;
esac
exit 0
EOF
cat > "$WBIN/curl" <<'EOF'
#!/bin/bash
echo 200; exit 0
EOF
cat > "$WBIN/systemctl" <<'EOF'
#!/bin/bash
case "$1" in is-active) exit 0;; start|restart) exit 0;; esac
exit 0
EOF
chmod +x "$WBIN"/*
w_setup() {
    rm -rf "$WST" "$WVOL"; mkdir -p "$WST" "$WVOL"
    : > "$CALLS"; : > "$WLOG"
    echo "wd-rbx1" > "$T/docker_ps"
    mkdir -p "$WVOL/wd-rbx1-data/_data/data/media/0/jerusalem"
    touch "$WVOL/wd-rbx1-data/_data/data/media/0/jerusalem/booted"
    touch "$WVOL/wd-rbx1-data/_data/data/media/0/jerusalem/live.png"
}
w_base() { echo "$WVOL/wd-rbx1-data/_data/data/media/0/jerusalem"; }
w_run() {
    DF_ST="$WST" DF_LOG="$WLOG" DF_INSTANCES="rbx1" DF_VOLBASE="$WVOL" \
      DF_LOCKDIR="$WST/lock" \
      PATH="$WBIN:/usr/bin:/bin" bash "$WD_SRC" >/dev/null 2>&1
}
degraded_lines() { grep -c "DEGRADED: Roblox keepalive" "$WLOG" 2>/dev/null; }

echo "== W1: keepalive.degraded -> instance marker + single log line =="
w_setup
echo "Roblox launch failing (14 attempts in the last hour) - auto-launch backed off, retrying every 5 min" > "$(w_base)/keepalive.degraded"
w_run
[ -f "$WST/rbx1.degraded" ] && ok "W1 instance marker created" || bad "W1 no marker"
head -1 "$WST/rbx1.degraded" | grep -q "^Roblox keepalive: Roblox launch failing" && ok "W1 reason mirrored" || bad "W1 reason: $(head -1 "$WST/rbx1.degraded")"
[ "$(degraded_lines)" = 1 ] && ok "W1 one DEGRADED log line" || bad "W1 log lines=$(degraded_lines)"

echo "== W2: rerun with same marker -> no log spam, marker kept =="
w_run
[ "$(degraded_lines)" = 1 ] && ok "W2 no duplicate log line" || bad "W2 log lines=$(degraded_lines)"
[ -f "$WST/rbx1.degraded" ] && ok "W2 marker kept" || bad "W2 marker dropped"

echo "== W3: marker cleared on recovery -> auto-clear + recovered log =="
rm -f "$(w_base)/keepalive.degraded"
w_run
[ ! -f "$WST/rbx1.degraded" ] && ok "W3 marker auto-cleared" || bad "W3 marker stuck"
grep -q "recovered from DEGRADED" "$WLOG" && ok "W3 recovered logged" || bad "W3 no recovered line"

echo "== W4: no keepalive marker -> healthy, no marker (regression) =="
w_setup; w_run
[ ! -f "$WST/rbx1.degraded" ] && ok "W4 no marker when healthy" || bad "W4 spurious marker"
[ "$(degraded_lines)" = 0 ] && ok "W4 no DEGRADED log" || bad "W4 spurious log"

echo "== W5: empty keepalive.degraded -> default reason =="
w_setup; : > "$(w_base)/keepalive.degraded"
w_run
head -1 "$WST/rbx1.degraded" | grep -q "launch attempts over budget" && ok "W5 default reason" || bad "W5 reason: $(head -1 "$WST/rbx1.degraded" 2>/dev/null)"

echo "== W6: pre-existing circuit-breaker marker is not clobbered =="
w_setup
echo "container restarted 7 times in last 3600s - auto-recovery paused" > "$WST/rbx1.degraded"
echo "Roblox launch failing (20 attempts in the last hour) - backed off" > "$(w_base)/keepalive.degraded"
w_run
head -1 "$WST/rbx1.degraded" | grep -q "^container restarted" && ok "W6 cb marker preserved" || bad "W6 marker: $(head -1 "$WST/rbx1.degraded")"
[ "$(degraded_lines)" = 0 ] && ok "W6 no keepalive log while cb marker present" || bad "W6 spurious log"

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
