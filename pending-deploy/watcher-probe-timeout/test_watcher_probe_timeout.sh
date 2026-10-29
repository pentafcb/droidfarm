#!/bin/bash
# test_watcher_probe_timeout.sh — unit tests for the watcher-probe-timeout
# package (bounded `waydroid shell` probes in booted-watcher.sh +
# entrypoint.sh). Runs the real scripts against a fake `waydroid` with hang
# modes. Hermetic. Every hanging-probe test is wrapped in an outer `timeout`
# so a broken (unbounded) product fails the test instead of hanging it.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/booted-watcher.sh"
ENTRY="$HERE/../../waydroid-image/entrypoint.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export T

echo "== syntax =="
bash -n "$SRC" && ok "watcher syntax" || bad "watcher syntax"
bash -n "$ENTRY" && ok "entrypoint syntax" || bad "entrypoint syntax"

# ---------- fake waydroid (hang modes via $T/hang_getprop, $T/hang_pm) ----------
BIN="$T/fakebin"; mkdir -p "$BIN"
cat > "$BIN/waydroid" <<'EOF'
#!/bin/sh
# $T/hang_getprop -> sleep 120 on `shell getprop`; $T/hang_pm -> sleep 120 on `shell pm`
echo "waydroid $*" >> "$T/waydroid.log"
if [ "$1 $2" = "shell getprop" ]; then
  [ -f "$T/hang_getprop" ] && sleep 120
  v=$(cat "$T/bootval" 2>/dev/null) || exit 1
  [ "$v" = "FAIL" ] && exit 1
  echo "$v"; exit 0
fi
if [ "$1 $2 $3" = "shell pm list" ]; then
  [ -f "$T/hang_pm" ] && sleep 120
  [ "$(cat "$T/pmval" 2>/dev/null || echo 0)" = "1" ] && { echo "package:/data/app/com.roblox.client.apk"; exit 0; }
  exit 1
fi
exit 0
EOF
chmod +x "$BIN/waydroid"

# notimeout dir: every tool the watcher needs EXCEPT timeout (T6 fallback)
NOTIME="$T/notimeout"; mkdir -p "$NOTIME"
for b in grep mkdir touch sleep cat chmod rm mktemp stat date; do
  [ -x "/bin/$b" ] && ln -sf "/bin/$b" "$NOTIME/$b"
  [ -x "/usr/bin/$b" ] && ln -sf "/usr/bin/$b" "$NOTIME/$b"
done

bw_setup() { rm -rf "$T/df" "$T/hang_getprop" "$T/hang_pm"; mkdir -p "$T/df"; : > "$T/waydroid.log"; echo "0" > "$T/pmval"; echo "0" > "$T/bootval"; }
# bw_run <iters> [extra env]: bounded real-script run, outer timeout 40s guards
# against a broken (unbounded) product. Output captured to $T/out.
bw_run() {
  local iters="$1"; shift
  JERUSALEM_DATA_DIR="$T/df" BOOTED_WATCHER_INTERVAL=0 \
    BOOTED_WATCHER_MAX_ITERS="$iters" BOOTED_WATCHER_PROBE_TIMEOUT_S=2 \
    PATH="$BIN:/usr/bin:/bin" \
    timeout 40 sh "$SRC" > "$T/out" 2>&1
  echo "$?" > "$T/rc"
}

echo "== T1: hung getprop probe is bounded, no marker, WARN, exit 0 =="
bw_setup; touch "$T/hang_getprop"; echo "1" > "$T/bootval"  # would boot if probed
start=$(date +%s)
bw_run 1
elapsed=$(( $(date +%s) - start )); rc=$(cat "$T/rc")
[ "$rc" = 0 ] && ok "T1 completes (rc 0, not killed by outer timeout)" || bad "T1 rc=$rc (outer timeout killed it = product hung)"
[ "$elapsed" -lt 25 ] && ok "T1 bounded (~${elapsed}s, not 120s)" || bad "T1 took ${elapsed}s"
[ ! -f "$T/df/booted" ] && ok "T1 no marker on hung probe" || bad "T1 marker written despite hang"
grep -q "WARN: boot probe timed out" "$T/out" && ok "T1 timeout WARN logged" || bad "T1 no WARN"

echo "== T2: hung pm probe is bounded, no roblox_done, WARN =="
bw_setup; touch "$T/df/booted"; touch "$T/hang_pm"; echo "1" > "$T/pmval"
start=$(date +%s)
bw_run 1
elapsed=$(( $(date +%s) - start )); rc=$(cat "$T/rc")
[ "$rc" = 0 ] && ok "T2 completes (rc 0)" || bad "T2 rc=$rc"
[ "$elapsed" -lt 25 ] && ok "T2 bounded (~${elapsed}s)" || bad "T2 took ${elapsed}s"
[ ! -f "$T/df/roblox_done" ] && ok "T2 no roblox_done on hung probe" || bad "T2 roblox_done written despite hang"
grep -q "WARN: package probe timed out" "$T/out" && ok "T2 timeout WARN logged" || bad "T2 no WARN"

echo "== T3: normal getprop still writes the marker (regression) =="
bw_setup; echo "1" > "$T/bootval"
bw_run 2; rc=$(cat "$T/rc")
[ "$rc" = 0 ] && [ -f "$T/df/booted" ] && ok "T3 marker written on boot" || bad "T3 rc=$rc marker=$([ -f "$T/df/booted" ] && echo yes || echo no)"
! grep -q "timed out" "$T/out" && ok "T3 no spurious timeout WARN" || bad "T3 spurious WARN"

echo "== T4: normal pm still backfills roblox_done (regression) =="
bw_setup; touch "$T/df/booted"; echo "1" > "$T/pmval"
bw_run 2; rc=$(cat "$T/rc")
[ "$rc" = 0 ] && [ -f "$T/df/roblox_done" ] && ok "T4 roblox_done backfilled" || bad "T4 failed"

echo "== T5: failing (non-hung) probes stay silent, no marker =="
bw_setup; echo "FAIL" > "$T/bootval"
bw_run 2; rc=$(cat "$T/rc")
[ "$rc" = 0 ] && [ ! -f "$T/df/booted" ] && ok "T5 no marker on failure" || bad "T5 failed"
! grep -q "timed out" "$T/out" && ok "T5 no timeout WARN on fast failure" || bad "T5 spurious WARN"

echo "== T6: no timeout binary -> fallback still works =="
bw_setup; echo "1" > "$T/bootval"
# NB: absolute /usr/bin/timeout — the PATH prefix below hides `timeout`
# from the script under test, and bash would not find a bare `timeout`.
JERUSALEM_DATA_DIR="$T/df" BOOTED_WATCHER_INTERVAL=0 BOOTED_WATCHER_MAX_ITERS=2 \
  BOOTED_WATCHER_PROBE_TIMEOUT_S=2 PATH="$BIN:$NOTIME" \
  /usr/bin/timeout 40 /bin/sh "$SRC" > "$T/out" 2>&1; rc=$?
[ "$rc" = 0 ] && [ -f "$T/df/booted" ] && ok "T6 fallback writes marker" || bad "T6 rc=$rc"

echo "== T7: garbage timeout env -> default, still works =="
bw_setup; echo "1" > "$T/bootval"
JERUSALEM_DATA_DIR="$T/df" BOOTED_WATCHER_INTERVAL=0 BOOTED_WATCHER_MAX_ITERS=2 \
  BOOTED_WATCHER_PROBE_TIMEOUT_S="junk" PATH="$BIN:/usr/bin:/bin" \
  timeout 40 sh "$SRC" > "$T/out" 2>&1; rc=$?
[ "$rc" = 0 ] && [ -f "$T/df/booted" ] && ok "T7 garbage env tolerated" || bad "T7 rc=$rc"

echo "== T8: bw() unit — 124 on timeout, passthrough otherwise =="
# Extract and source just the bw definition + timeout env from the real script.
bw_src=$(sed -n '/^BOOTED_WATCHER_PROBE_TIMEOUT_S=/,/^}$/p' "$SRC" | sed -n '1,2p;/^if command -v timeout/,/^}$/p')
eval "$bw_src"
BOOTED_WATCHER_PROBE_TIMEOUT_S=1
bw sleep 5; rc=$?
[ "$rc" = "124" ] && ok "T8 bw returns 124 on timeout" || bad "T8 rc=$rc"
bw true; rc=$?
[ "$rc" = "0" ] && ok "T8 bw passes through rc 0" || bad "T8 rc=$rc"
bw sh -c 'exit 3'; rc=$?
[ "$rc" = "3" ] && ok "T8 bw passes through rc 3" || bad "T8 rc=$rc"

echo "== T9: entrypoint boot-wait probe is timeout-bounded =="
grep -q "timeout 10 waydroid shell getprop sys.boot_completed" "$ENTRY" \
  && ok "T9 entrypoint probe wrapped in timeout 10" || bad "T9 no timeout on entrypoint probe"
# Functional: extract the wait loop to a file (avoids outer-shell expansion),
# run against a hanging waydroid — must finish (not hang) with BOOTED=0.
bw_setup; touch "$T/hang_getprop"
loop_src=$(awk '/for i in \$\(seq 1 60\); do/{f=1} f{print} f&&/done/{exit}' "$ENTRY" \
  | sed 's/seq 1 60/seq 1 3/; s/sleep 5/sleep 0/')
[ -n "$loop_src" ] || bad "T9 loop extraction failed"
printf 'BOOTED=0\n%s\necho "BOOTED=$BOOTED"\n' "$loop_src" > "$T/loop.sh"
start=$(date +%s)
PATH="$BIN:/usr/bin:/bin" timeout 60 sh "$T/loop.sh" > "$T/out" 2>&1; rc=$?
elapsed=$(( $(date +%s) - start ))
[ "$rc" = 0 ] && ok "T9 loop completes against hung waydroid" || bad "T9 rc=$rc (hung)"
[ "$elapsed" -lt 50 ] && ok "T9 bounded (~${elapsed}s for 3 iters)" || bad "T9 took ${elapsed}s"
grep -q "BOOTED=0" "$T/out" && ok "T9 BOOTED=0 after timeouts" || bad "T9 wrong BOOTED"

echo "== T10: both markers present -> probes skipped (steady state cheap) =="
bw_setup; touch "$T/df/booted"; touch "$T/df/roblox_done"
touch "$T/hang_getprop"; touch "$T/hang_pm"
bw_run 2; rc=$(cat "$T/rc")
[ "$rc" = 0 ] && ok "T10 completes" || bad "T10 rc=$rc"
[ "$(grep -c "shell getprop\|shell pm" "$T/waydroid.log")" = 0 ] \
  && ok "T10 no probes when markers present" || bad "T10 probed despite markers"

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
