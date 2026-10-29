#!/bin/bash
# test_push_timeout.sh — hermetic tests for the push-script timeout hardening.
#
# Drives the REAL push-keepalive.sh / push-booted-watcher.sh / push-screencap.sh /
# push-optimizer.sh with a fake docker (argv log, hang modes, canned ps output)
# and fake volume trees. Verifies: bounded docker calls (no silent hangs),
# fail-closed docker-ps, [i/N] progress lines, timeout-binary fallback,
# DEX_TIMEOUT sanitizing, and that no unbounded docker call sites remain.
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PASS=0; FAILC=0
ok()  { PASS=$((PASS+1)); }
bad() { FAILC=$((FAILC+1)); echo "TEST-FAIL: $*"; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; mkdir -p "$BIN"
SRC="$T/src"; mkdir -p "$SRC"
VOL="$T/vol"

for s in roblox-keepalive.sh booted-watcher.sh screencap-loop.sh android-optimize.sh; do
    printf '#!/bin/bash\necho fake-src\n' > "$SRC/$s"
done
for c in wd-a wd-b; do
    d="$VOL/$c-data/_data/data/media/0/jerusalem"
    mkdir -p "$d"; touch "$d/booted"
done

# --- fake docker: argv log + hang modes + canned responses -------------------
cat > "$BIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "${DOCKER_ARGV_LOG:-/dev/null}"
sub="${1:-}"
case "${DOCKER_HANG:-}" in
  ps)   [ "$sub" = "ps" ] && exec sleep 30 ;;
  exec) [ "$sub" = "exec" ] && exec sleep 30 ;;
  cp)   [ "$sub" = "cp" ] && exec sleep 30 ;;
  exec:pgrep)
    if [ "$sub" = "exec" ]; then
      case " $* " in *pgrep*) exec sleep 30 ;; esac
    fi ;;
esac
[ -n "${DOCKER_FAIL_PS:-}" ] && [ "$sub" = "ps" ] && exit 1
case "$sub" in
  ps) printf 'wd-a\nwd-b\n' ;;
  cp) exit 0 ;;
  exec)
    cname="$2"
    case " $* " in
      *android-optimize.sh*)
        lf="${DOCKER_VOLBASE:-}/$cname-data/_data/data/media/0/jerusalem/optimize.log"
        { echo "[optimize] run at fake"; echo "[optimize] done"; } >> "$lf" ;;
    esac
    case " $* " in
      *pgrep*) [ -n "${PGREP_FAIL:-}" ] && exit 1 ;;
    esac
    exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$BIN/docker"

# --- fake timeout: logs the bound, then execs the real binary ------------------
cat > "$BIN/timeout" <<'EOF2'
#!/bin/bash
echo "timeout $*" >> "${DOCKER_ARGV_LOG:-/dev/null}"
exec /usr/bin/timeout "$@"
EOF2
chmod +x "$BIN/timeout"

# --- PATH without a timeout binary (fallback test) ---------------------------
NB="$T/nobin"; mkdir -p "$NB"
for b in bash sh sleep grep sort cp chmod tail wc; do
    p=$(command -v "$b" 2>/dev/null) && ln -sf "$p" "$NB/$b"
done
ln -sf "$BIN/docker" "$NB/docker"

PKEEP="$REPO/pending-deploy/keepalive-v2/push-keepalive.sh"
PWATCH="$REPO/pending-deploy/booted-watcher/push-booted-watcher.sh"
PSCAP="$REPO/pending-deploy/adaptive-screencap/push-screencap.sh"
POPT="$REPO/pending-deploy/android-optimizer/push-optimizer.sh"

# --- 1. keepalive healthy path ------------------------------------------------
export DOCKER_ARGV_LOG="$T/argv1.log" DOCKER_VOLBASE="$VOL" DOCKER_HANG="" DOCKER_FAIL_PS="" PGREP_FAIL=""
out=$(PATH="$BIN:/usr/bin:/bin" VOLBASE="$VOL" bash "$PKEEP" "$SRC/roblox-keepalive.sh" 2>&1); rc=$?
[ "$rc" = 0 ] && ok "keepalive healthy exit 0" || bad "keepalive healthy exit $rc"
echo "$out" | grep -q '\[1/2\] wd-a: keepalive v2 running' && ok "keepalive [1/2] line" || bad "keepalive [1/2] missing: $out"
echo "$out" | grep -q '\[2/2\] wd-b: keepalive v2 running' && ok "keepalive [2/2] line" || bad "keepalive [2/2] missing"
echo "$out" | grep -q 'pushed to 2 instance(s)' && ok "keepalive summary" || bad "keepalive summary missing"
grep -q 'timeout 30 docker exec' "$T/argv1.log" && ok "keepalive dex wraps exec" || bad "keepalive exec not timeout-wrapped"
grep -q 'timeout 30 docker ps' "$T/argv1.log" && ok "keepalive dex wraps ps" || bad "keepalive ps not timeout-wrapped"

# --- 2. keepalive: hung docker ps fails closed and bounded --------------------
SECONDS=0
out=$(DOCKER_HANG=ps DEX_TIMEOUT=1 PATH="$BIN:/usr/bin:/bin" VOLBASE="$VOL" bash "$PKEEP" "$SRC/roblox-keepalive.sh" 2>&1); rc=$?
dur=$SECONDS
[ "$rc" = 1 ] && ok "hung ps exit 1" || bad "hung ps exit $rc"
echo "$out" | grep -q 'FAIL: docker ps failed' && ok "hung ps FAIL line" || bad "hung ps FAIL line missing"
echo "$out" | grep -q 'daemon may be wedged' && ok "hung ps names the cause" || bad "hung ps cause missing"
[ "$dur" -lt 10 ] && ok "hung ps bounded (${dur}s)" || bad "hung ps took ${dur}s"

# --- 3. keepalive: docker ps rc!=0 fails closed --------------------------------
out=$(DOCKER_HANG="" DOCKER_FAIL_PS=1 PATH="$BIN:/usr/bin:/bin" VOLBASE="$VOL" bash "$PKEEP" "$SRC/roblox-keepalive.sh" 2>&1); rc=$?
[ "$rc" = 1 ] && ok "ps rc!=0 exit 1" || bad "ps rc!=0 exit $rc"
echo "$out" | grep -q 'FAIL: docker ps failed' && ok "ps rc!=0 FAIL line" || bad "ps rc!=0 FAIL line missing"

# --- 4. keepalive: hung verify-pgrep fails the instance, bounded --------------
SECONDS=0
out=$(DOCKER_HANG=exec:pgrep DEX_TIMEOUT=1 PATH="$BIN:/usr/bin:/bin" VOLBASE="$VOL" bash "$PKEEP" "$SRC/roblox-keepalive.sh" 2>&1); rc=$?
dur=$SECONDS
[ "$rc" = 1 ] && ok "hung pgrep exit 1" || bad "hung pgrep exit $rc"
echo "$out" | grep -q '\[1/2\] wd-a: keepalive restart FAILED' && ok "hung pgrep instance FAILED" || bad "hung pgrep FAILED line missing"
[ "$dur" -lt 25 ] && ok "hung pgrep bounded (${dur}s)" || bad "hung pgrep took ${dur}s"

# --- 5. keepalive: no timeout binary -> bare-docker fallback --------------------
export DOCKER_ARGV_LOG="$T/argv5.log" DOCKER_HANG="" DOCKER_FAIL_PS=""
out=$(PATH="$NB" VOLBASE="$VOL" bash "$PKEEP" "$SRC/roblox-keepalive.sh" 2>&1); rc=$?
[ "$rc" = 0 ] && ok "no-timeout fallback exit 0" || bad "no-timeout fallback exit $rc"
echo "$out" | grep -q 'keepalive v2 running' && ok "no-timeout still pushes" || bad "no-timeout push missing"
grep -q '^docker ps' "$T/argv5.log" && ok "no-timeout bare docker ps" || bad "no-timeout ps not bare"
! grep -q 'timeout' "$T/argv5.log" && ok "no-timeout never calls timeout" || bad "no-timeout called timeout"

# --- 6. keepalive: garbage DEX_TIMEOUT sanitizes to 30 --------------------------
export DOCKER_ARGV_LOG="$T/argv6.log"
PATH="$BIN:/usr/bin:/bin" VOLBASE="$VOL" DEX_TIMEOUT=abc bash "$PKEEP" "$SRC/roblox-keepalive.sh" >/dev/null 2>&1
grep -q 'timeout 30 docker' "$T/argv6.log" && ok "garbage DEX_TIMEOUT -> 30" || bad "garbage DEX_TIMEOUT not sanitized"

# --- 7. booted-watcher healthy path ---------------------------------------------
export DOCKER_ARGV_LOG="$T/argv7.log" DOCKER_HANG="" DEX_TIMEOUT=30
out=$(PATH="$BIN:/usr/bin:/bin" bash "$PWATCH" "$SRC/booted-watcher.sh" 2>&1); rc=$?
[ "$rc" = 0 ] && ok "watcher healthy exit 0" || bad "watcher healthy exit $rc"
echo "$out" | grep -q '\[1/2\] wd-a: booted-watcher running' && ok "watcher [1/2] line" || bad "watcher [1/2] missing"
echo "$out" | grep -q 'started in 2 instance(s)' && ok "watcher summary" || bad "watcher summary missing"
grep -q 'timeout 30 docker cp' "$T/argv7.log" && ok "watcher dex wraps cp" || bad "watcher cp not timeout-wrapped"

# --- 8. booted-watcher: hung docker cp fails the instance, bounded --------------
SECONDS=0
out=$(DOCKER_HANG=cp DEX_TIMEOUT=1 PATH="$BIN:/usr/bin:/bin" bash "$PWATCH" "$SRC/booted-watcher.sh" 2>&1); rc=$?
dur=$SECONDS
[ "$rc" = 1 ] && ok "hung cp exit 1" || bad "hung cp exit $rc"
echo "$out" | grep -q '\[1/2\] wd-a: docker cp FAILED' && ok "hung cp instance FAILED" || bad "hung cp FAILED line missing"
[ "$dur" -lt 15 ] && ok "hung cp bounded (${dur}s)" || bad "hung cp took ${dur}s"

# --- 9. screencap healthy path --------------------------------------------------
export DOCKER_ARGV_LOG="$T/argv9.log" DOCKER_HANG=""
out=$(PATH="$BIN:/usr/bin:/bin" PUSH_SCREENCAP_SRC="$SRC/screencap-loop.sh" PUSH_SCREENCAP_VOLBASE="$VOL" bash "$PSCAP" 2>&1); rc=$?
[ "$rc" = 0 ] && ok "screencap healthy exit 0" || bad "screencap healthy exit $rc"
echo "$out" | grep -q '\[1/2\] a: adaptive loop running' && ok "screencap [1/2] line" || bad "screencap [1/2] missing: $out"
echo "$out" | grep -q 'pushed to 2 container(s)' && ok "screencap summary" || bad "screencap summary missing"
grep -q 'timeout 30 docker exec' "$T/argv9.log" && ok "screencap dex wraps exec" || bad "screencap exec not timeout-wrapped"

# --- 10. screencap: hung exec fails the instance, bounded -----------------------
SECONDS=0
out=$(DOCKER_HANG=exec DEX_TIMEOUT=1 PATH="$BIN:/usr/bin:/bin" PUSH_SCREENCAP_SRC="$SRC/screencap-loop.sh" PUSH_SCREENCAP_VOLBASE="$VOL" bash "$PSCAP" 2>&1); rc=$?
dur=$SECONDS
[ "$rc" = 1 ] && ok "screencap hung exec exit 1" || bad "screencap hung exec exit $rc"
echo "$out" | grep -q '\[1/2\] a: RESTART FAILED' && ok "screencap instance FAILED" || bad "screencap FAILED line missing"
[ "$dur" -lt 30 ] && ok "screencap hung bounded (${dur}s)" || bad "screencap hung took ${dur}s"

# --- 11. optimizer healthy path --------------------------------------------------
export DOCKER_ARGV_LOG="$T/argv11.log" DOCKER_HANG=""
out=$(PATH="$BIN:/usr/bin:/bin" VOLBASE="$VOL" DOCKER_VOLBASE="$VOL" bash "$POPT" "$SRC/android-optimize.sh" 2>&1); rc=$?
[ "$rc" = 0 ] && ok "optimizer healthy exit 0" || bad "optimizer healthy exit $rc"
echo "$out" | grep -q '\[1/2\] wd-a: optimizer applied' && ok "optimizer [1/2] line" || bad "optimizer [1/2] missing: $out"
echo "$out" | grep -q 'applied to 2 instance(s), skipped 0' && ok "optimizer summary" || bad "optimizer summary missing"
grep -q 'timeout 30 docker ps' "$T/argv11.log" && ok "optimizer dex wraps ps" || bad "optimizer ps not timeout-wrapped"
grep -q 'timeout 60 docker exec' "$T/argv11.log" && ok "optimizer keeps its 60s waydroid bound" || bad "optimizer waydroid bound changed"

# --- 12. optimizer: hung docker ps fails closed and bounded -----------------------
SECONDS=0
out=$(DOCKER_HANG=ps DEX_TIMEOUT=1 PATH="$BIN:/usr/bin:/bin" VOLBASE="$VOL" DOCKER_VOLBASE="$VOL" bash "$POPT" "$SRC/android-optimize.sh" 2>&1); rc=$?
dur=$SECONDS
[ "$rc" = 1 ] && ok "optimizer hung ps exit 1" || bad "optimizer hung ps exit $rc"
echo "$out" | grep -q 'FAIL: docker ps failed' && ok "optimizer hung ps FAIL line" || bad "optimizer hung ps FAIL line missing"
[ "$dur" -lt 10 ] && ok "optimizer hung ps bounded (${dur}s)" || bad "optimizer hung ps took ${dur}s"

# --- 13. source contracts: no unbounded docker call sites -------------------------
for s in "$PKEEP" "$PWATCH" "$PSCAP" "$POPT"; do
    n=$(basename "$s")
    code=$(sed 's/"[^"]*"//g' "$s")   # drop quoted message strings
    echo "$code" | grep -q 'dex ps' && ok "$n: listing via dex" || bad "$n: listing not via dex"
    echo "$code" | grep -q '$(docker ps' && bad "$n: bare \$(docker ps) remains" || ok "$n: no bare \$(docker ps)"
    if [ -z "$(echo "$code" | grep -n 'docker exec' | grep -v 'dex exec' | grep -v 'timeout  docker exec')" ]; then
        ok "$n: every docker exec is dex/timeout-bounded"
    else
        bad "$n: unbounded docker exec remains"
    fi
    if [ -z "$(echo "$code" | grep -n 'docker cp' | grep -v 'dex cp')" ]; then
        ok "$n: every docker cp is dex-bounded"
    else
        bad "$n: unbounded docker cp remains"
    fi
    grep -q 'DEX_TIMEOUT="${DEX_TIMEOUT:-30}"' "$s" && ok "$n: DEX_TIMEOUT default 30" || bad "$n: DEX_TIMEOUT default missing"
    bash -n "$s" && ok "$n: bash -n clean" || bad "$n: bash -n failed"
done

echo
echo "$PASS passed, $FAILC failed"
[ "$FAILC" = 0 ]
