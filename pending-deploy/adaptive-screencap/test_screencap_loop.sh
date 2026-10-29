#!/bin/bash
# Hermetic tests for waydroid-image/screencap-loop.sh: run the REAL script
# with a fake screencap (instant) and a fake sleep (logs durations), plus
# real stat/date. Asserts the adaptive rate: fast (0.12s) with a fresh
# stream_wanted marker, slow (2s) when the marker is missing or stale.
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
LOOP="$REPO/waydroid-image/screencap-loop.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1 $2"; }

sh -n "$LOOP" || { bad "sh -n syntax" ""; echo "$PASS passed, $FAIL failed"; exit 1; }
ok "sh -n syntax clean"

run_loop() { # $1 = marker state: fresh|stale|absent ; echoes sleep log path
  local state=$1
  local t; t=$(mktemp -d)
  local fakebin="$t/fakebin"; mkdir -p "$fakebin"
  cat > "$fakebin/screencap" <<'EOF'
#!/bin/sh
# fake screencap: instant, "captures" by touching the -p target
while [ $# -gt 0 ]; do
  case "$1" in -p) touch "$2" 2>/dev/null; shift 2;; *) shift;; esac
done
EOF
  cat > "$fakebin/sleep" <<EOF
#!/bin/sh
echo "\$1" >> "$t/sleeps.log"
EOF
  chmod +x "$fakebin/screencap" "$fakebin/sleep"
  mkdir -p "$t/jerusalem"
  case "$state" in
    fresh) touch "$t/jerusalem/stream_wanted" ;;
    stale) touch -d '5 minutes ago' "$t/jerusalem/stream_wanted" ;;
  esac
  SCREENCAP_DIR="$t/jerusalem" PATH="$fakebin:$PATH" timeout 4 sh "$LOOP" >/dev/null 2>&1
  echo "$t"
}

# --- 1. fresh marker -> fast captures
t=$(run_loop fresh)
n=$(tr -d ' ' < "$t/sleeps.log" | grep -c .)
badn=$(awk '$1 != "0.12"' "$t/sleeps.log" | wc -l)
if [ "$n" -ge 2 ] && [ "$badn" -eq 0 ]; then ok "fresh marker: $n fast (0.12s) captures"; else bad "fresh marker fast captures" "n=$n non-0.12=$badn"; fi
rm -rf "$t"

# --- 2. absent marker -> slow captures
t=$(run_loop absent)
n=$(tr -d ' ' < "$t/sleeps.log" | grep -c .)
badn=$(awk '$1 != "2"' "$t/sleeps.log" | wc -l)
if [ "$n" -ge 1 ] && [ "$badn" -eq 0 ]; then ok "absent marker: $n slow (2s) captures"; else bad "absent marker slow captures" "n=$n non-2=$badn"; fi
[ -f "$t/jerusalem/live.png" ] && ok "live.png still written when idle" || bad "live.png written when idle" ""
rm -rf "$t"

# --- 3. stale marker -> slow captures (manager/agent restart safety)
t=$(run_loop stale)
n=$(tr -d ' ' < "$t/sleeps.log" | grep -c .)
badn=$(awk '$1 != "2"' "$t/sleeps.log" | wc -l)
if [ "$n" -ge 1 ] && [ "$badn" -eq 0 ]; then ok "stale marker: $n slow (2s) captures, no fast pin"; else bad "stale marker slow captures" "n=$n non-2=$badn"; fi
rm -rf "$t"

echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
