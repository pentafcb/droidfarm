#!/bin/bash
# test_watchdog_v12.sh — unit tests for df-watchdog.sh v12 (manager probe
# requires a COMPLETE response body). Runs the real watchdog against a fake
# `docker` + fake `curl` (honors -o, can truncate with exit 18) + fake
# `systemctl` + temp state/log/volume dirs. No real services are touched.
# Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
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

# ---- fake curl: honors -o <file>; $T/curl_code wins (default 200);
#      $T/curl_fail -> connect timeout (000/rc 28); $T/curl_trunc ->
#      200 header but mid-body close (partial body, rc 18) -------------------
cat > "$BIN/curl" <<'EOF'
#!/bin/bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [ -f "$T/curl_fail" ]; then echo "000"; exit 28; fi
code="$(cat "$T/curl_code" 2>/dev/null || echo 200)"
if [ -f "$T/curl_trunc" ]; then
  [ -n "$out" ] && printf 'PARTIAL-BODY' > "$out"   # truncated: 12 of N bytes
  echo "200"; exit 18
fi
[ -n "$out" ] && printf 'FULL-BODY-....................................................................................................' > "$out"
echo "$code"
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
  rm -f "$T/curl_fail" "$T/curl_code" "$T/curl_trunc" "$T/svc_active" "$T/svc_fail"
  echo "wd-rbx1" > "$T/docker_ps"          # healthy container
  : > "$T/pgrep_dead"                       # all helpers alive
  date -u +%FT%TZ > "$T/docker_started_at"
  local d="$VOL/wd-rbx1-data/_data/data/media/0/jerusalem"
  mkdir -p "$d"; touch "$d/booted" "$d/live.png"
}
run_wd() { # run_wd [extra env assignments]: run the watchdog once
  DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="rbx1" DF_VOLBASE="$VOL" \
    DF_LOCKDIR="$T/lock" \
    PATH="$BIN:/usr/bin:/bin" "$@" bash "$SRC" >/dev/null 2>&1
}
seed_events() { # seed_events <key> <count> <age_secs>
  local f="$ST/$1.ev" n=$2 age=$3 now i
  now=$(date +%s); : > "$f"
  for i in $(seq 1 "$n"); do echo $((now - age)) >> "$f"; done
}
no_probe_tmp() { # no_probe_tmp <label>: probe temp files cleaned up
  if ls "$ST"/mgr.probe.* >/dev/null 2>&1; then bad "$1 probe temp files left behind"; else ok "$1 probe temp files cleaned"; fi
}

echo "== bash -n =="
bash -n "$SRC" && ok "syntax" || bad "syntax"

echo "== T1 complete 200: healthy, silent, no temp files =="
setup
echo 200 > "$T/curl_code"; touch "$T/svc_active"
run_wd
grep -q "systemctl" "$CALLS" && bad "T1 systemctl called" || ok "T1 no systemctl call"
grep -q -i "manager" "$LOG" && bad "T1 manager mentioned in log" || ok "T1 log silent"
[ -f "$ST/mgr.httpdown" ] && bad "T1 strike file present" || ok "T1 no strike"
no_probe_tmp "T1"

echo "== T2 truncated 200 (rc 18): strike 1, distinct log line, no restart =="
setup
touch "$T/curl_trunc" "$T/svc_active"
run_wd
grep -q "systemctl restart\|systemctl start" "$CALLS" && bad "T2 restart on strike 1" || ok "T2 no restart on strike 1"
grep -q "body truncated" "$LOG" && ok "T2 truncation logged" || bad "T2 truncation not logged"
grep -q "curl rc 18" "$LOG" && ok "T2 curl rc in log" || bad "T2 curl rc missing from log"
grep -q "strike 1/2" "$LOG" && ok "T2 strike 1/2 logged" || bad "T2 strike 1/2 not logged"
[ "$(cat "$ST/mgr.httpdown" 2>/dev/null)" = "1" ] && ok "T2 strike counted" || bad "T2 strike not counted"
no_probe_tmp "T2"

echo "== T3 truncated twice: one restart, strikes reset =="
setup
touch "$T/curl_trunc" "$T/svc_active"
run_wd; run_wd
[ "$(grep -c 'systemctl restart jerusalem' "$CALLS")" -eq 1 ] && ok "T3 one restart on strike 2" || bad "T3 expected exactly one restart"
[ -f "$ST/mgr.httpdown" ] && bad "T3 strikes not reset" || ok "T3 strikes reset"
grep -q "manager restarted ok" "$LOG" && ok "T3 restart success logged" || bad "T3 restart success not logged"
grep -q "systemctl start jerusalem" "$CALLS" && bad "T3 used start instead of restart" || ok "T3 restart (not start) when active"

echo "== T4 truncated then complete: no restart, strike cleared =="
setup
touch "$T/curl_trunc" "$T/svc_active"
run_wd
rm -f "$T/curl_trunc"; echo 200 > "$T/curl_code"
run_wd
grep -q "systemctl restart\|systemctl start" "$CALLS" && bad "T4 restart after recovery" || ok "T4 no restart after recovery"
[ -f "$ST/mgr.httpdown" ] && bad "T4 strike not cleared" || ok "T4 strike cleared"

echo "== T5 complete 500: old code-path wording, no truncation line =="
setup
echo 500 > "$T/curl_code"; touch "$T/svc_active"
run_wd
grep -q "probe failed (code 500, strike 1/2)" "$LOG" && ok "T5 code-failure wording kept" || bad "T5 code-failure wording changed"
grep -q "body truncated" "$LOG" && bad "T5 truncation line on complete 500" || ok "T5 no truncation line on complete 500"

echo "== T6 connect timeout (rc 28, 000): code path, not truncation =="
setup
touch "$T/curl_fail" "$T/svc_active"
run_wd
grep -q "probe failed (code 000, strike 1/2)" "$LOG" && ok "T6 timeout wording kept" || bad "T6 timeout wording changed"
grep -q "body truncated" "$LOG" && bad "T6 truncation line on timeout" || ok "T6 no truncation line on timeout"

echo "== T7 truncated probe does NOT auto-clear degraded =="
setup
touch "$T/curl_trunc" "$T/svc_active"
echo "manager service restarted 3 times - auto-recovery paused" > "$ST/manager.degraded"
run_wd
[ -f "$ST/manager.degraded" ] && ok "T7 degraded NOT cleared by truncated probe" || bad "T7 degraded cleared by truncated probe"
[ -f "$ST/mgr.httpdown" ] && ok "T7 strike counted while degraded" || bad "T7 strike missing while degraded"
rm -f "$T/curl_trunc"; echo 200 > "$T/curl_code"
run_wd
[ -f "$ST/manager.degraded" ] && bad "T7 degraded not cleared by complete probe" || ok "T7 degraded cleared by complete probe"
grep -q "manager recovered from DEGRADED" "$LOG" && ok "T7 recovery logged" || bad "T7 recovery not logged"

echo "== T8 truncation storm: circuit breaker trips, reason names manager =="
setup
touch "$T/curl_trunc" "$T/svc_active"
seed_events "mgr.restart" 2 60
echo 2 > "$ST/mgr.httpdown"
run_wd
grep -q "systemctl restart\|systemctl start" "$CALLS" && bad "T8 restart attempted past budget" || ok "T8 no restart past budget"
[ -f "$ST/manager.degraded" ] && ok "T8 degraded marker set" || bad "T8 degraded marker missing"
head -1 "$ST/manager.degraded" | grep -q "manager service" && ok "T8 reason names manager service" || bad "T8 reason missing/wrong"
DEG_LINES=$(grep -c "DEGRADED" "$LOG")
run_wd
[ "$(grep -c "DEGRADED" "$LOG")" -eq "$DEG_LINES" ] && ok "T8 no DEGRADED log spam" || bad "T8 DEGRADED logged again"

echo "== T9 complete 302: still healthy (v5 regression) =="
setup
echo 302 > "$T/curl_code"; touch "$T/svc_active"
run_wd
grep -q "systemctl" "$CALLS" && bad "T9 systemctl called on 302" || ok "T9 no action on complete 302"
grep -q "body truncated" "$LOG" && bad "T9 truncation line on complete 302" || ok "T9 no truncation line"

echo "== T10 curl missing: skip unchanged (v5 regression) =="
setup
touch "$T/curl_trunc" "$T/svc_active"
run_wd env "DF_CURL_BIN=/nonexistent/curl"
run_wd env "DF_CURL_BIN=/nonexistent/curl"
grep -q "systemctl" "$CALLS" && bad "T10 systemctl called without curl" || ok "T10 no restart without probe tool"
[ "$(grep -c "curl .* missing" "$LOG")" -eq 1 ] && ok "T10 single WARN line (per-day)" || bad "T10 WARN not deduped"
[ -f "$ST/mgr.httpdown" ] && bad "T10 strike counted without probe" || ok "T10 no strike counted"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
