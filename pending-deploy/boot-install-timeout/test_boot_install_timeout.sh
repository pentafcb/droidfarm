#!/bin/bash
# test_boot_install_timeout.sh — unit tests for bounded install-block calls.
#
# GAP IT COVERS: waydroid-image/boot-complete.sh ran three FOREGROUND
# `waydroid shell` calls unbounded (pm list packages, pm install of the
# ~150MB split APKs, input keyevent 4). A wedged session hangs `waydroid
# shell` forever, which wedged the whole boot sequence AFTER the `booted`
# marker was written — the dashboard showed "ready", the stuck-boot check
# never fired (marker present), and the instance sat with no helpers and
# no Roblox: a silent wedge. The fix wraps the three calls in bcs()
# (timeout-bounded, fallback to plain call); a timeout WARNs and the
# script continues so the helpers still start.
#
# Tests: the real boot-complete.sh vs a fake waydroid with hang modes.
# Outer `timeout` guards every run so a broken (unbounded) product fails
# the suite instead of hanging it.
# Exit 0 when all checks pass, 1 otherwise. Final line: "<N> passed, 0 failed".
HERE="$(cd "$(dirname "$0")" && pwd)"
BC="$HERE/../../waydroid-image/boot-complete.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export T
BIN="$T/bin"; mkdir -p "$BIN"

echo "== syntax =="
bash -n "$BC" && ok "boot-complete.sh syntax" || bad "boot-complete.sh syntax"

# ---------- fake waydroid with hang modes + fake pgrep ----------
cat > "$BIN/waydroid" <<'EOF'
#!/bin/sh
echo "waydroid $*" >> "$T/waydroid.log"
# hang flags: $T/hang_pm_list, $T/hang_pm_install, $T/hang_keyevent
if [ "$1 $2" = "shell pm" ] && [ "$3" = "list" ]; then
  [ -f "$T/hang_pm_list" ] && exec sleep 300
  [ "$(cat "$T/pm_installed" 2>/dev/null || echo 0)" = "1" ] && echo "package:/data/app/com.roblox.client.apk"
  exit 0
fi
if [ "$1 $2" = "shell pm" ] && [ "$3" = "install" ]; then
  [ -f "$T/hang_pm_install" ] && exec sleep 300
  if [ "$(cat "$T/pm_install_fail" 2>/dev/null || echo 0)" = "1" ]; then
    echo "Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]"
  else
    echo "Success"
  fi
  exit 0
fi
if [ "$1 $2 $3" = "shell input keyevent" ] && [ "$4" = "4" ]; then
  [ -f "$T/hang_keyevent" ] && exec sleep 300
  exit 0
fi
exit 0
EOF
chmod +x "$BIN/waydroid"
cat > "$BIN/pgrep" <<'EOF'
#!/bin/sh
grep -qx "$2" "$T/pgrep_running" 2>/dev/null
EOF
chmod +x "$BIN/pgrep"

# notimeout dir: every tool the script needs EXCEPT timeout (T5 fallback)
NOTIME="$T/notimeout"; mkdir -p "$NOTIME"
for b in grep mkdir touch cp chmod rm cat sleep date nohup; do
  [ -x "/bin/$b" ] && ln -sf "/bin/$b" "$NOTIME/$b"
  [ -x "/usr/bin/$b" ] && ln -sf "/usr/bin/$b" "$NOTIME/$b"
done

bit_setup() { # fresh data dir, fake /opt, no hang flags, APK present
  rm -rf "$T/df" "$T/opt" "$T/hang_pm_list" "$T/hang_pm_install" "$T/hang_keyevent"
  mkdir -p "$T/df" "$T/opt"
  : > "$T/waydroid.log"; : > "$T/pgrep_running"
  echo 0 > "$T/pm_installed"; echo 0 > "$T/pm_install_fail"
  echo "fake" > "$T/opt/roblox-base.apk"; echo "fake" > "$T/opt/roblox-x86_64.apk"
  echo "fake" > "$T/opt/screencap-loop.sh"; echo "fake" > "$T/opt/roblox-keepalive.sh"
  echo "fake" > "$T/opt/android-optimize.sh"
}
bit_run() { # bit_run: bounded real-script run (2s timeouts), outer guard 60s.
  # $1 = extra PATH prefix (default "$BIN:/usr/bin:/bin")
  local path="${1:-$BIN:/usr/bin:/bin}"
  JERUSALEM_DATA_DIR="$T/df" BOOT_COMPLETE_OPT="$T/opt" \
    BC_PM_LIST_TIMEOUT_S=2 BC_PM_INSTALL_TIMEOUT_S=2 BC_KEYEVENT_TIMEOUT_S=2 \
    PATH="$path" \
    /usr/bin/timeout 60 /bin/sh "$BC" > "$T/out" 2>&1
  echo "$?" > "$T/rc"
  # let backgrounded helper-starts land in waydroid.log, then quiet them
  sleep 0.5
  pkill -f "sleep 300" 2>/dev/null || true
}

echo "== T1: happy path regression — install succeeds, helpers start =="
bit_setup
bit_run
rc=$(cat "$T/rc")
[ "$rc" = "0" ] && ok "T1 rc=0" || bad "T1 rc=$rc"
[ -f "$T/df/roblox_done" ] && ok "T1 roblox_done on Success" || bad "T1 no roblox_done"
[ -f "$T/df/screencap-loop.sh" ] && [ -f "$T/df/input-daemon.sh" ] \
  && [ -f "$T/df/roblox-keepalive.sh" ] && ok "T1 helpers installed" || bad "T1 helpers missing"
grep -q "Roblox installed successfully" "$T/out" && ok "T1 success logged" || bad "T1 no success log"
grep -q "boot-complete done" "$T/out" && ok "T1 completes" || bad "T1 did not complete"

echo "== T2: pm list hangs -> bounded, install still attempted =="
bit_setup; touch "$T/hang_pm_list"
start=$(date +%s); bit_run; end=$(date +%s)
rc=$(cat "$T/rc")
[ "$rc" = "0" ] && ok "T2 rc=0 (not killed by outer guard)" || bad "T2 rc=$rc (outer timeout?)"
[ $((end - start)) -lt 20 ] && ok "T2 bounded (~$((end - start))s, not wedged)" || bad "T2 took $((end - start))s"
grep -q "pm list packages timed out" "$T/out" && ok "T2 timeout WARN logged" || bad "T2 no timeout WARN"
grep -q "waydroid shell pm install" "$T/waydroid.log" && ok "T2 install attempted after list timeout" || bad "T2 install not attempted"
[ -f "$T/df/roblox_done" ] && ok "T2 roblox_done (install succeeded)" || bad "T2 no roblox_done"
grep -q "boot-complete done" "$T/out" && ok "T2 completes" || bad "T2 did not complete"

echo "== T3: pm install hangs -> bounded, WARN, helpers STILL start =="
bit_setup; touch "$T/hang_pm_install"
start=$(date +%s); bit_run; end=$(date +%s)
rc=$(cat "$T/rc")
[ "$rc" = "0" ] && ok "T3 rc=0 (not killed by outer guard)" || bad "T3 rc=$rc (outer timeout?)"
[ $((end - start)) -lt 20 ] && ok "T3 bounded (~$((end - start))s, not wedged)" || bad "T3 took $((end - start))s"
grep -q "Roblox install timed out after 2s" "$T/out" && ok "T3 timeout WARN names the cause" || bad "T3 no timeout WARN"
[ ! -f "$T/df/roblox_done" ] && ok "T3 no roblox_done on timeout" || bad "T3 roblox_done on timeout"
[ -f "$T/df/screencap-loop.sh" ] && [ -f "$T/df/input-daemon.sh" ] \
  && [ -f "$T/df/roblox-keepalive.sh" ] && ok "T3 helpers installed despite hung install" || bad "T3 helpers missing"
grep -q "waydroid shell sh /data/media/0/jerusalem/screencap-loop.sh" "$T/waydroid.log" \
  && ok "T3 screencap helper started" || bad "T3 screencap not started"
grep -q "boot-complete done" "$T/out" && ok "T3 completes past the hung install" || bad "T3 did not complete"

echo "== T4: keyevent hangs -> bounded, WARN, completes =="
bit_setup; echo 1 > "$T/pm_installed"; touch "$T/hang_keyevent"
start=$(date +%s); bit_run; end=$(date +%s)
rc=$(cat "$T/rc")
[ "$rc" = "0" ] && ok "T4 rc=0" || bad "T4 rc=$rc"
[ $((end - start)) -lt 20 ] && ok "T4 bounded (~$((end - start))s)" || bad "T4 took $((end - start))s"
grep -q "dismiss keyevent timed out" "$T/out" && ok "T4 timeout WARN logged" || bad "T4 no WARN"
grep -q "boot-complete done" "$T/out" && ok "T4 completes" || bad "T4 did not complete"

echo "== T5: no timeout binary -> fallback, normal path works =="
bit_setup
JERUSALEM_DATA_DIR="$T/df" BOOT_COMPLETE_OPT="$T/opt" \
  BC_PM_LIST_TIMEOUT_S=2 BC_PM_INSTALL_TIMEOUT_S=2 BC_KEYEVENT_TIMEOUT_S=2 \
  PATH="$BIN:$NOTIME" \
  /usr/bin/timeout 60 /bin/sh "$BC" > "$T/out" 2>&1; rc=$?
sleep 0.5
[ "$rc" = "0" ] && ok "T5 rc=0" || bad "T5 rc=$rc"
[ -f "$T/df/roblox_done" ] && ok "T5 fallback installs fine" || bad "T5 fallback broken"
grep -q "boot-complete done" "$T/out" && ok "T5 completes" || bad "T5 did not complete"

echo "== T6: garbage timeout env -> no hang, completes =="
bit_setup
JERUSALEM_DATA_DIR="$T/df" BOOT_COMPLETE_OPT="$T/opt" \
  BC_PM_LIST_TIMEOUT_S="junk" BC_PM_INSTALL_TIMEOUT_S="junk" BC_KEYEVENT_TIMEOUT_S="junk" \
  PATH="$BIN:/usr/bin:/bin" \
  /usr/bin/timeout 60 /bin/sh "$BC" > "$T/out" 2>&1; rc=$?
sleep 0.5
[ "$rc" = "0" ] && ok "T6 rc=0 (garbage tolerated)" || bad "T6 rc=$rc"
grep -q "boot-complete done" "$T/out" && ok "T6 completes" || bad "T6 did not complete"

echo "== T7: source contracts =="
grep -q '^bcs() {' "$BC" && ok "T7 bcs() defined" || bad "T7 no bcs()"
grep -q 'bcs "$BC_PM_LIST_TIMEOUT_S" shell pm list packages' "$BC" \
  && ok "T7 pm list bounded" || bad "T7 pm list not bounded"
grep -q 'bcs "$BC_PM_INSTALL_TIMEOUT_S" shell pm install' "$BC" \
  && ok "T7 pm install bounded" || bad "T7 pm install not bounded"
grep -q 'bcs "$BC_KEYEVENT_TIMEOUT_S" shell input keyevent 4' "$BC" \
  && ok "T7 keyevent bounded" || bad "T7 keyevent not bounded"
# backgrounded calls must NOT be wrapped (they cannot wedge the script)
grep 'waydroid shell.*&$' "$BC" | grep -q 'bcs' \
  && bad "T7 backgrounded call wrapped (unnecessary)" || ok "T7 backgrounded calls untouched"
# no RAW (unbounded, foreground) `waydroid shell` left: filter comments,
# bcs()-wrapped calls, and backgrounded (&) calls; anything left is a bug
bad_lines=$(grep -n 'waydroid shell' "$BC" | grep -v '^[^:]*:#' | grep -v 'bcs "' | grep -v '&$' || true)
[ -z "$bad_lines" ] && ok "T7 no unbounded foreground waydroid shell" || bad "T7 unbounded: $bad_lines"
grep -q 'BC_PM_INSTALL_TIMEOUT_S:-300' "$BC" && ok "T7 install default 300s" || bad "T7 bad default"

echo "== T8: bcs() unit — 124 on timeout, passthrough otherwise =="
bcs_src=$(sed -n '/^BC_PM_LIST_TIMEOUT_S=/,/^}$/p' "$BC")
eval "$bcs_src"
export PATH="$BIN:/usr/bin:/bin"
touch "$T/hang_pm_list"
bcs 1 shell pm list packages >/dev/null 2>&1; rc=$?
[ "$rc" = "124" ] && ok "T8 bcs returns 124 on timeout" || bad "T8 rc=$rc"
rm -f "$T/hang_pm_list"
bcs 5 shell pm list packages >/dev/null 2>&1; rc=$?
[ "$rc" = "0" ] && ok "T8 bcs passthrough rc=0" || bad "T8 passthrough rc=$rc"
# fallback path: hide `timeout` from PATH, plain call still works
( PATH="$BIN:$NOTIME"; bcs 5 shell pm list packages >/dev/null 2>&1; echo "rc=$?" ) \
  | grep -q "rc=0" && ok "T8 fallback without timeout binary" || bad "T8 fallback broken"
pkill -f "sleep 300" 2>/dev/null || true

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
