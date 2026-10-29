#!/bin/bash
# test_booted_watcher.sh — unit tests for waydroid-image/booted-watcher.sh.
# Runs the real script (bounded via BOOTED_WATCHER_MAX_ITERS) against a fake
# `waydroid`. Hermetic. Also checks the entrypoint/Dockerfile/push wiring.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/booted-watcher.sh"
ENTRY="$HERE/../../waydroid-image/entrypoint.sh"
DOCKERFILE="$HERE/../../waydroid-image/Dockerfile"
PUSH="$HERE/push-booted-watcher.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export T

echo "== syntax =="
bash -n "$SRC" && ok "watcher syntax" || bad "watcher syntax"
bash -n "$ENTRY" && ok "entrypoint syntax" || bad "entrypoint syntax"
bash -n "$PUSH" && ok "push script syntax" || bad "push script syntax"

# ---------- fake waydroid ----------
BIN="$T/fakebin"; mkdir -p "$BIN"
cat > "$BIN/waydroid" <<'EOF'
#!/bin/sh
# scenario: $T/bootval for getprop (FAIL -> exit 1), $T/pmval for pm (1 = installed)
echo "waydroid $*" >> "$T/waydroid.log"
if [ "$1 $2" = "shell getprop" ]; then
  v=$(cat "$T/bootval" 2>/dev/null) || exit 1
  [ "$v" = "FAIL" ] && exit 1
  echo "$v"; exit 0
fi
if [ "$1 $2 $3" = "shell pm list" ]; then
  [ "$(cat "$T/pmval" 2>/dev/null || echo 0)" = "1" ] && { echo "package:/data/app/com.roblox.client.apk"; exit 0; }
  exit 1
fi
exit 0
EOF
chmod +x "$BIN/waydroid"

bw_run() { # bw_run <iters>: bounded run of the real script
  JERUSALEM_DATA_DIR="$T/df" BOOTED_WATCHER_INTERVAL=0 BOOTED_WATCHER_MAX_ITERS="$1" \
    PATH="$BIN:/usr/bin:/bin" sh "$SRC" >/dev/null 2>&1
  return $?
}
bw_setup() { rm -rf "$T/df"; mkdir -p "$T/df"; : > "$T/waydroid.log"; echo "0" > "$T/pmval"; }
probes() { grep -c "shell getprop" "$T/waydroid.log" 2>/dev/null; }

echo "== W1: late boot -> marker written =="
bw_setup; echo "1" > "$T/bootval"
bw_run 2
[ -f "$T/df/booted" ] && ok "W1 booted marker written on late boot" || bad "W1 no marker"

echo "== W2: not booted -> no marker =="
bw_setup; echo "0" > "$T/bootval"
bw_run 2
[ ! -f "$T/df/booted" ] && ok "W2 no marker while boot incomplete" || bad "W2 marker written early"

echo "== W3: waydroid fails (session dead) -> no marker, no crash =="
bw_setup; echo "FAIL" > "$T/bootval"
bw_run 2; rc=$?
[ ! -f "$T/df/booted" ] && ok "W3 no marker on waydroid failure" || bad "W3 marker on failure"
[ "$rc" = 0 ] && ok "W3 exits 0 (never crashes)" || bad "W3 rc=$rc"

echo "== W4: garbage getprop output -> no marker =="
bw_setup; echo "1junk" > "$T/bootval"
bw_run 2
[ ! -f "$T/df/booted" ] && ok "W4 garbage output ignored" || bad "W4 marker on garbage"

echo "== W5: marker present -> waydroid never probed (cheap steady state) =="
bw_setup; touch "$T/df/booted"; echo "FAIL" > "$T/bootval"  # would fail if probed
bw_run 3
[ "$(probes)" = 0 ] && ok "W5 no probe when marker present" || bad "W5 probed $(probes)x"
[ -f "$T/df/booted" ] && ok "W5 marker untouched" || bad "W5 marker removed!"

echo "== W6: roblox_done backfilled when client present =="
bw_setup; touch "$T/df/booted"; echo "1" > "$T/pmval"; echo "0" > "$T/bootval"
bw_run 2
[ -f "$T/df/roblox_done" ] && ok "W6 roblox_done backfilled" || bad "W6 no roblox_done"

echo "== W7: roblox absent -> no roblox_done =="
bw_setup; touch "$T/df/booted"; echo "0" > "$T/pmval"
bw_run 2
[ ! -f "$T/df/roblox_done" ] && ok "W7 no roblox_done when client missing" || bad "W7 spurious roblox_done"

echo "== W8: idempotent across runs, never deletes =="
bw_setup; echo "1" > "$T/bootval"; echo "1" > "$T/pmval"
bw_run 2
[ -f "$T/df/booted" ] && [ -f "$T/df/roblox_done" ] || { bad "W8 setup failed"; }
echo "FAIL" > "$T/bootval"; echo "0" > "$T/pmval"  # everything fails now
bw_run 2; rc=$?
[ -f "$T/df/booted" ] && ok "W8 marker survives failing probes" || bad "W8 marker deleted"
[ "$rc" = 0 ] && ok "W8 still exits 0" || bad "W8 rc=$rc"
! grep -q "rm .\{0,20\}booted" "$SRC" && ok "W8 script contains no marker deletion" || bad "W8 script deletes markers"

echo "== W9: entrypoint starts the watcher =="
grep -q "nohup /opt/booted-watcher.sh" "$ENTRY" && ok "W9 entrypoint starts watcher" || bad "W9 no start line"
grep -q "WARN /opt/booted-watcher.sh missing" "$ENTRY" && ok "W9 old-image WARN fallback" || bad "W9 no WARN fallback"
grep -q "if \[ -f /opt/booted-watcher.sh \]" "$ENTRY" && ok "W9 guarded by file check" || bad "W9 no file guard"

echo "== W10: Dockerfile bakes the script =="
grep -q "^COPY booted-watcher.sh /opt/booted-watcher.sh" "$DOCKERFILE" && ok "W10 Dockerfile COPY" || bad "W10 no COPY line"

echo "== W11: push script fail-closed =="
"$PUSH" /nonexistent/booted-watcher.sh >/dev/null 2>&1; rc=$?
[ "$rc" != 0 ] && ok "W11 missing source fails" || bad "W11 missing source passed"
"$PUSH" /nonexistent/booted-watcher.sh 2>&1 | grep -q "FAIL: source missing" && ok "W11 clear error message" || bad "W11 no error message"
grep -q "grep '^wd-'" "$PUSH" && ok "W11 only targets wd-* containers" || bad "W11 container filter"
grep -q "pkill -f booted-watcher.sh" "$PUSH" && ok "W11 idempotent re-push" || bad "W11 no re-push handling"

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
