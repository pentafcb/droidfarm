#!/bin/bash
# test_keepalive_probe_timeout.sh — unit tests for roblox-keepalive.sh v3
# (bounded probes + unknown-skip + wedged-session surfacing) and the
# v8/1.0.15 keepalive.degraded mirror contract (reason on the first line).
# Runs the real script against fake dumpsys/pm/input with hang modes.
# Hermetic. Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
KA_SRC="$HERE/../../waydroid-image/roblox-keepalive.sh"
PASS=0; FAIL=0

ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export T

echo "== bash -n =="
bash -n "$KA_SRC" && ok "keepalive syntax" || bad "keepalive syntax"

# ---------- fake Android tools ----------
BIN="$T/fakebin"; mkdir -p "$BIN"
cat > "$BIN/dumpsys" <<'EOF'
#!/bin/sh
# $T/dumpsys_mode: focus string | FAIL | HANG
# $T/dumpsys_hang_left: hang this many calls (then behave per mode)
m=$(cat "$T/dumpsys_mode" 2>/dev/null)
left=$(cat "$T/dumpsys_hang_left" 2>/dev/null || echo 0)
if [ "$left" -gt 0 ] 2>/dev/null; then
  echo $((left - 1)) > "$T/dumpsys_hang_left"
  sleep 300
fi
[ "$m" = "HANG" ] && sleep 300
[ "$m" = "FAIL" ] && exit 1
echo "mCurrentFocus=Window{abcd u0 $m}"
EOF
cat > "$BIN/input" <<'EOF'
#!/bin/sh
# log BEFORE any hang: proves the launch sequence continued past a hung cmd
echo "$*" >> "$T/input.log"
[ "$(cat "$T/input_hang" 2>/dev/null)" = "1" ] && sleep 300
EOF
cat > "$BIN/pm" <<'EOF'
#!/bin/sh
# $T/pm_code: 0 = installed | 1 = missing | HANG
c=$(cat "$T/pm_code" 2>/dev/null || echo 0)
[ "$c" = "HANG" ] && sleep 300
[ "$c" = "0" ] && { echo "package:/data/app/com.roblox.client.apk"; exit 0; }
exit 1
EOF
chmod +x "$BIN"/*

ka_run() { # ka_run <iters> [budget] [probe_timeout] [probe_fails]: bounded run
    local iters=$1 budget=${2:-12} pto=${3:-1} pf=${4:-3}
    KA_DIR="$T/df" KA_TICK_S=0 KA_LAUNCH_SLEEP=0 KA_MAX_ITERS="$iters" \
      KA_MAX_PER_HOUR="$budget" KA_BACKOFF_S=0 \
      KA_PROBE_TIMEOUT_S="$pto" KA_PROBE_FAILS="$pf" \
      PATH="$BIN:/usr/bin:/bin" sh "$KA_SRC" >/dev/null 2>&1
}
ka_setup() { # fresh scenario
    rm -rf "$T/df"; mkdir -p "$T/df"
    : > "$T/input.log"
    echo "0" > "$T/pm_code"
    echo "0" > "$T/dumpsys_hang_left"
    rm -f "$T/input_hang"
}
input_count() { grep -c "^$1$" "$T/input.log" 2>/dev/null; }

FOCUS_RBX="com.roblox.client/com.roblox.client.MainActivity"
FOCUS_HOME="com.android.launcher3/com.android.launcher3.Launcher"

echo "== T1: focused -> no taps, no state, no marker (v2 regression) =="
ka_setup; echo "$FOCUS_RBX" > "$T/dumpsys_mode"
ka_run 3
[ "$(input_count "keyevent 3")" = "0" ] && ok "no taps when focused" || bad "taps fired while focused"
[ ! -f "$T/df/keepalive.state" ] && ok "no state when focused" || bad "state written while focused"
[ ! -f "$T/df/keepalive.degraded" ] && ok "no marker when focused" || bad "marker written while focused"

echo "== T2: not focused, fast probes -> taps + attempt counted (v2 regression) =="
ka_setup; echo "$FOCUS_HOME" > "$T/dumpsys_mode"
ka_run 2
[ "$(input_count "keyevent 3")" = "2" ] && ok "launch taps issued" || bad "launch taps missing"
[ "$(input_count "tap 129 424")" = "2" ] && ok "icon tap coords intact" || bad "icon tap coords changed"
[ -f "$T/df/keepalive.state" ] && ok "attempt counted" || bad "attempt not counted"
[ ! -f "$T/df/keepalive.degraded" ] && ok "no marker under budget" || bad "marker under budget"

echo "== T3: dumpsys hangs x2 (< 3 fails) -> bounded, no taps, no attempt, no marker =="
ka_setup; echo "HANG" > "$T/dumpsys_mode"
s=$(date +%s); ka_run 2; e=$(date +%s)
[ $((e - s)) -lt 20 ] && ok "hung probe bounded (took $((e-s))s)" || bad "loop hung on wedged dumpsys"
[ "$(input_count "keyevent 3")" = "0" ] && ok "no taps on unknown probe" || bad "tapped on unknown probe"
[ ! -f "$T/df/keepalive.state" ] && ok "no attempt counted on unknown" || bad "attempt counted on unknown probe"
[ ! -f "$T/df/keepalive.degraded" ] && ok "no marker below fail threshold" || bad "marker below fail threshold"

echo "== T4: dumpsys hangs x4 -> probe-degraded marker, reason on first line =="
ka_setup; echo "HANG" > "$T/dumpsys_mode"
ka_run 4
[ -f "$T/df/keepalive.degraded" ] && ok "probe-degraded marker written" || bad "no probe-degraded marker"
head -1 "$T/df/keepalive.degraded" | grep -qi "unresponsive\|probe" \
  && ok "reason names the probe timeout" || bad "reason does not name the cause"
[ "$(head -c 160 "$T/df/keepalive.degraded" | head -1 | wc -c)" -le 161 ] \
  && ok "first line fits the 160-char mirror contract" || bad "first line too long for the mirror"
head -1 "$T/df/keepalive.degraded" | grep -q "launch failing" \
  && bad "probe marker reused the budget reason" || ok "probe reason distinct from budget reason"

echo "== T5: recovery clears the probe-degraded marker =="
ka_setup; echo "HANG" > "$T/dumpsys_mode"
ka_run 4
[ -f "$T/df/keepalive.degraded" ] || bad "setup: marker not written"
echo "$FOCUS_RBX" > "$T/dumpsys_mode"
ka_run 2
[ ! -f "$T/df/keepalive.degraded" ] && ok "marker cleared on recovery" || bad "marker stuck after recovery"
[ "$(input_count "keyevent 3")" = "0" ] && ok "no taps after recovery to focused" || bad "taps after recovery"

echo "== T6: pm hangs -> conservative skip, never trips the focus marker =="
ka_setup; echo "$FOCUS_HOME" > "$T/dumpsys_mode"; echo "HANG" > "$T/pm_code"
ka_run 2
[ "$(input_count "keyevent 3")" = "0" ] && ok "no taps when pm probe unknown" || bad "tapped on unknown pm probe"
[ ! -f "$T/df/keepalive.degraded" ] && ok "no marker below threshold (pm)" || bad "marker below threshold (pm)"
[ ! -f "$T/df/keepalive.state" ] && ok "no attempt counted on pm unknown" || bad "attempt counted on pm unknown"
ka_setup; echo "$FOCUS_HOME" > "$T/dumpsys_mode"; echo "HANG" > "$T/pm_code"
ka_run 5
[ ! -f "$T/df/keepalive.degraded" ] && ok "pm hangs never trip the focus marker" || bad "pm hang tripped the focus marker"
ka_setup; echo "$FOCUS_HOME" > "$T/dumpsys_mode"; echo "1" > "$T/pm_code"
ka_run 4
[ ! -f "$T/df/keepalive.degraded" ] && ok "fast 'not installed' never trips the marker" || bad "not-installed tripped the marker"

echo "== T7: input hangs mid-launch -> bounded, sequence continues =="
ka_setup; echo "$FOCUS_HOME" > "$T/dumpsys_mode"; echo "1" > "$T/input_hang"
s=$(date +%s); ka_run 1; e=$(date +%s)
[ $((e - s)) -lt 20 ] && ok "hung input bounded (took $((e-s))s)" || bad "loop hung on wedged input"
[ "$(input_count "keyevent 3")" = "1" ] && ok "keyevent attempted" || bad "keyevent missing"
[ "$(input_count "swipe 512 400 512 100")" = "1" ] && ok "swipe attempted after hung keyevent" || bad "sequence stopped at hung input"
[ "$(input_count "tap 129 424")" = "1" ] && ok "tap attempted after hung input" || bad "tap missing after hung input"

echo "== T8: no timeout binary -> fallback, loop still works =="
NTB="$T/notimeout"; mkdir -p "$NTB"
for t in mkdir date grep sleep rm cat; do
  p=$(command -v "$t" 2>/dev/null) && ln -sf "$p" "$NTB/$t"
done
ka_setup; echo "$FOCUS_RBX" > "$T/dumpsys_mode"
KA_DIR="$T/df" KA_TICK_S=0 KA_LAUNCH_SLEEP=0 KA_MAX_ITERS=2 \
  KA_MAX_PER_HOUR=12 KA_BACKOFF_S=0 \
  PATH="$NTB:$BIN" sh "$KA_SRC" >/dev/null 2>&1
[ "$(input_count "keyevent 3")" = "0" ] && ok "fallback: no taps when focused" || bad "fallback: taps while focused"
[ ! -f "$T/df/keepalive.degraded" ] && ok "fallback: no marker" || bad "fallback: marker written"

echo "== T9: budget path intact (v2 regression) =="
ka_setup; echo "$FOCUS_HOME" > "$T/dumpsys_mode"
ka_run 4 2
[ -f "$T/df/keepalive.degraded" ] && ok "budget marker written" || bad "budget marker missing"
head -1 "$T/df/keepalive.degraded" | grep -q "attempts in the last hour" \
  && ok "budget reason intact" || bad "budget reason changed"

echo "== T10: garbage env -> sane defaults, loop runs =="
ka_setup; echo "$FOCUS_RBX" > "$T/dumpsys_mode"
KA_DIR="$T/df" KA_TICK_S=0 KA_LAUNCH_SLEEP=0 KA_MAX_ITERS=2 \
  KA_PROBE_TIMEOUT_S=0 KA_PROBE_FAILS=bogus \
  PATH="$BIN:/usr/bin:/bin" sh "$KA_SRC" >/dev/null 2>&1
[ "$(input_count "keyevent 3")" = "0" ] && ok "garbage env: loop runs" || bad "garbage env broke the loop"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
