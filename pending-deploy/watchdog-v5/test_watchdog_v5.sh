#!/bin/bash
# test_watchdog_v5.sh — unit tests for df-watchdog.sh v5 (manager self-check).
# Runs the real watchdog against a fake `docker` + fake `curl` + fake
# `systemctl` + temp state/log/volume dirs. No real services are touched.
# Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
echo "5000 0" > "$T/uptime"  # pin high uptime: v14 boot recovery stays out of these check_one tests
BIN="$T/bin"; ST="$T/st"; VOL="$T/vol"; LOG="$T/watchdog.log"; CALLS="$T/calls.log"
mkdir -p "$BIN" "$ST" "$VOL"
export T CALLS

# ---- fake docker (healthy instance) ---------------------------------------
cat > "$BIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
sub=$1; shift
case "$sub" in
  ps)      cat "$T/docker_ps" 2>/dev/null ;;
  inspect) cat "$T/docker_started_at" 2>/dev/null ;;
  start|restart) exit 0 ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep) grep -qx "$3" "$T/pgrep_dead" 2>/dev/null && exit 1 || exit 0 ;;
      waydroid) # v9 session probe: `waydroid shell getprop sys.boot_completed`
        case "$*" in *getprop*) [ -f "$T/session_dead" ] && exit 1; echo 1; exit 0;; esac
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
EOF
chmod +x "$BIN/docker"

# ---- fake curl: $T/curl_code wins (default 200); $T/curl_fail -> timeout ----
cat > "$BIN/curl" <<'EOF'
#!/bin/bash
if [ -f "$T/curl_fail" ]; then echo "000"; exit 28; fi
cat "$T/curl_code" 2>/dev/null || echo 200
exit 0
EOF
chmod +x "$BIN/curl"

# ---- fake systemctl: active iff $T/svc_active; $T/svc_fail breaks start ----
cat > "$BIN/systemctl" <<'EOF'
#!/bin/bash
echo "systemctl $*" >> "$CALLS"
case "$1" in
  is-active) [ -f "$T/svc_active" ] && exit 0 || exit 3 ;;
  start|restart) [ -f "$T/svc_fail" ] && exit 1; exit 0 ;;
esac
exit 0
EOF
chmod +x "$BIN/systemctl"

# ---- scenario setup --------------------------------------------------------
setup() {
  rm -rf "$ST" "$VOL"; mkdir -p "$ST" "$VOL"
  : > "$CALLS"; : > "$LOG"
  rm -f "$T/curl_fail" "$T/curl_code" "$T/svc_active" "$T/svc_fail"
  echo "wd-rbx1" > "$T/docker_ps"          # healthy container
  : > "$T/pgrep_dead"                       # all helpers alive
  date -u +%FT%TZ > "$T/docker_started_at"
  local d="$VOL/wd-rbx1-data/_data/data/media/0/jerusalem"
  mkdir -p "$d"; touch "$d/booted" "$d/live.png"
}
run_wd() { # run_wd [extra env assignments]: run the watchdog once
  DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="rbx1" DF_VOLBASE="$VOL" \
    DF_LOCKDIR="$T/lock" DF_PROC_UPTIME="$T/uptime" \
    PATH="$BIN:/usr/bin:/bin" "$@" bash "$SRC" >/dev/null 2>&1
}
seed_events() { # seed_events <key> <count> <age_secs>
  local f="$ST/$1.ev" n=$2 age=$3 now i
  now=$(date +%s); : > "$f"
  for i in $(seq 1 "$n"); do echo $((now - age)) >> "$f"; done
}

echo "== bash -n =="
bash -n "$SRC" && ok "syntax" || bad "syntax"

echo "== T1 healthy manager (200): no action =="
setup
echo 200 > "$T/curl_code"; touch "$T/svc_active"
run_wd
grep -q "systemctl" "$CALLS" && bad "T1 systemctl called" || ok "T1 no systemctl call"
grep -q -i "manager" "$LOG" && bad "T1 manager mentioned in log" || ok "T1 log silent"
[ -f "$ST/manager.degraded" ] && bad "T1 degraded" || ok "T1 not degraded"

echo "== T2 wedged manager (500, active): 2 strikes -> restart =="
setup
echo 500 > "$T/curl_code"; touch "$T/svc_active"
run_wd
grep -q "systemctl restart" "$CALLS" && bad "T2 restart on strike 1" || ok "T2 no restart on strike 1"
grep -q "strike 1/2" "$LOG" && ok "T2 strike 1 logged" || bad "T2 strike 1 not logged"
run_wd
[ "$(grep -c 'systemctl restart jerusalem' "$CALLS")" -eq 1 ] && ok "T2 one restart on strike 2" || bad "T2 expected exactly one restart"
grep -q "systemctl start jerusalem" "$CALLS" && bad "T2 used start instead of restart" || ok "T2 restart (not start) when active"
[ -f "$ST/mgr.httpdown" ] && bad "T2 strikes not reset" || ok "T2 strikes reset"
[ "$(wc -l < "$ST/mgr.restart.ev")" -eq 1 ] && ok "T2 restart counted in budget" || bad "T2 restart not counted"
grep -q "manager restarted ok" "$LOG" && ok "T2 restart success logged" || bad "T2 restart success not logged"

echo "== T3 manager down (curl fail, service inactive): -> start =="
setup
touch "$T/curl_fail"   # no svc_active -> inactive
run_wd; run_wd
[ "$(grep -c 'systemctl start jerusalem' "$CALLS")" -eq 1 ] && ok "T3 one start on strike 2" || bad "T3 expected exactly one start"
grep -q "systemctl restart jerusalem" "$CALLS" && bad "T3 used restart instead of start" || ok "T3 start (not restart) when inactive"

echo "== T4 redirect (302): treated healthy =="
setup
echo 302 > "$T/curl_code"; touch "$T/svc_active"
run_wd
grep -q "systemctl" "$CALLS" && bad "T4 systemctl called on 302" || ok "T4 no action on 302"

echo "== T5 restart storm -> manager DEGRADED, no more restarts =="
setup
echo 500 > "$T/curl_code"; touch "$T/svc_active"
seed_events "mgr.restart" 2 60
echo 2 > "$ST/mgr.httpdown"
run_wd
grep -q "systemctl restart\|systemctl start" "$CALLS" && bad "T5 restart attempted past budget" || ok "T5 no restart past budget"
[ -f "$ST/manager.degraded" ] && ok "T5 degraded marker set" || bad "T5 degraded marker missing"
head -1 "$ST/manager.degraded" | grep -q "manager service" && ok "T5 reason names manager service" || bad "T5 reason missing/wrong"
DEG_LINES=$(grep -c "DEGRADED" "$LOG")
run_wd
grep -q "systemctl restart\|systemctl start" "$CALLS" && bad "T5 restart while degraded" || ok "T5 still no restart while degraded"
[ "$(grep -c "DEGRADED" "$LOG")" -eq "$DEG_LINES" ] && ok "T5 no DEGRADED log spam" || bad "T5 DEGRADED logged again"

echo "== T6 degraded auto-clear when probe healthy =="
setup
echo 200 > "$T/curl_code"; touch "$T/svc_active"
echo "manager service restarted 3 times - auto-recovery paused" > "$ST/manager.degraded"
seed_events "mgr.restart" 2 60
run_wd
[ -f "$ST/manager.degraded" ] && bad "T6 degraded not cleared" || ok "T6 degraded cleared"
ls "$ST"/mgr.*.ev >/dev/null 2>&1 && bad "T6 event files not cleaned" || ok "T6 event files cleaned"
grep -q "manager recovered from DEGRADED" "$LOG" && ok "T6 recovery logged" || bad "T6 recovery not logged"

echo "== T7 curl missing: skip with one WARN per day =="
setup
echo 500 > "$T/curl_code"; touch "$T/svc_active"
run_wd env "DF_CURL_BIN=/nonexistent/curl"
run_wd env "DF_CURL_BIN=/nonexistent/curl"
grep -q "systemctl" "$CALLS" && bad "T7 systemctl called without curl" || ok "T7 no restart without probe tool"
[ "$(grep -c "curl .* missing" "$LOG")" -eq 1 ] && ok "T7 single WARN line (per-day)" || bad "T7 WARN not deduped"
[ -f "$ST/mgr.httpdown" ] && bad "T7 strike counted without probe" || ok "T7 no strike counted"

echo "== T8 failed restart is logged, not silent =="
setup
echo 500 > "$T/curl_code"; touch "$T/svc_active" "$T/svc_fail"
run_wd; run_wd
grep -q "manager restart FAILED" "$LOG" && ok "T8 restart failure logged" || bad "T8 restart failure not logged"

echo "== T9 manager-degraded does not block instance recovery =="
setup
echo 200 > "$T/curl_code"; touch "$T/svc_active"
echo "manager service restarted 3 times - auto-recovery paused" > "$ST/manager.degraded"
: > "$T/docker_ps"   # container down
run_wd; run_wd
grep -q "docker start wd-rbx1" "$CALLS" && ok "T9 instance recovery still runs" || bad "T9 instance recovery blocked"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
