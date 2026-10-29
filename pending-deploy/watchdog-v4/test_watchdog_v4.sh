#!/bin/bash
# test_watchdog_v4.sh — unit tests for df-watchdog.sh v4 (circuit breaker).
# Runs the real watchdog against a fake `docker` + temp state/log/volume dirs.
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
export T CALLS   # the fake docker reads scenario files + call log from these

# ---- fake docker -----------------------------------------------------------
cat > "$BIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
sub=$1; shift
case "$sub" in
  ps)      cat "$T/docker_ps" 2>/dev/null ;;
  inspect) cat "$T/docker_started_at" 2>/dev/null ;;
  start|restart)
    [ -f "$T/docker_fail" ] && exit 1
    exit 0 ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep) # $2=-f $3=name
        grep -qx "$3" "$T/pgrep_dead" 2>/dev/null && exit 1 || exit 0 ;;
      waydroid) # v9 session probe: `waydroid shell getprop sys.boot_completed`
        case "$*" in *getprop*) [ -f "$T/session_dead" ] && exit 1; echo 1; exit 0;; esac
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
EOF
chmod +x "$BIN/docker"

# ---- fake curl + systemctl (v5 manager self-check; keep the v4 suite
# hermetic: real curl would probe 127.0.0.1:8080 and real systemctl could
# act on the host running the tests) --------------------------------------
cat > "$BIN/curl" <<'EOF'
#!/bin/bash
cat "$T/curl_code" 2>/dev/null || echo 200
exit 0
EOF
chmod +x "$BIN/curl"
cat > "$BIN/systemctl" <<'EOF'
#!/bin/bash
echo "systemctl $*" >> "$CALLS"
case "$1" in
  is-active) exit 0 ;;
  start|restart) exit 0 ;;
esac
exit 0
EOF
chmod +x "$BIN/systemctl"

# ---- scenario setup --------------------------------------------------------
setup() { # setup: reset all scenario files
  rm -rf "$ST" "$VOL"; mkdir -p "$ST" "$VOL"
  : > "$CALLS"; : > "$LOG"
  rm -f "$T/docker_fail" "$T/pgrep_dead"
  : > "$T/docker_ps"; : > "$T/pgrep_dead"
  date -u +%FT%TZ > "$T/docker_started_at"
}
inst_base() { # inst_base <inst>: create the instance data dir, echo it
  local d="$VOL/wd-$1-data/_data/data/media/0/jerusalem"
  mkdir -p "$d"; echo "$d"
}
run_wd() { # run_wd <instances>: run the watchdog once
  DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="$1" DF_VOLBASE="$VOL" \
    DF_LOCKDIR="$T/lock" DF_PROC_UPTIME="$T/uptime" \
    PATH="$BIN:/usr/bin:/bin" bash "$SRC" >/dev/null 2>&1
}
seed_events() { # seed_events <key> <count> <age_secs>: N events, all <age> old
  local f="$ST/$1.ev" n=$2 age=$3 now i
  now=$(date +%s); : > "$f"
  for i in $(seq 1 "$n"); do echo $((now - age)) >> "$f"; done
}

echo "== bash -n =="
bash -n "$SRC" && ok "syntax" || bad "syntax"

echo "== T1 healthy instance: no action =="
setup
echo "wd-rbx1" > "$T/docker_ps"
B=$(inst_base rbx1); touch "$B/booted" "$B/live.png"
run_wd "rbx1"
grep -q "docker start\|docker restart\|nohup" "$CALLS" && bad "T1 unexpected recovery action" || ok "T1 no recovery action"
[ -f "$ST/rbx1.degraded" ] && bad "T1 degraded file created" || ok "T1 not degraded"

echo "== T2 container down: 2 strikes -> docker start =="
setup
run_wd "rbx1"   # strike 1
grep -q "docker start" "$CALLS" && bad "T2 started on first strike" || ok "T2 no start on strike 1"
run_wd "rbx1"   # strike 2
[ "$(grep -c 'docker start wd-rbx1' "$CALLS")" -eq 1 ] && ok "T2 one docker start on strike 2" || bad "T2 expected exactly one docker start"
[ -f "$ST/rbx1.degraded" ] && bad "T2 degraded" || ok "T2 not degraded"
[ -f "$ST/rbx1.norun" ] && bad "T2 strikes not reset" || ok "T2 strikes reset"

echo "== T3 restart storm -> DEGRADED, no more starts =="
setup
seed_events "rbx1.restart" 6 60
echo 2 > "$ST/rbx1.norun"   # already at 2 strikes
run_wd "rbx1"
if grep -q "docker start" "$CALLS"; then bad "T3 docker start attempted past budget"; else ok "T3 no start past budget"; fi
[ -f "$ST/rbx1.degraded" ] && ok "T3 degraded marker set" || bad "T3 degraded marker missing"
grep -q "DEGRADED" "$LOG" && ok "T3 DEGRADED logged" || bad "T3 DEGRADED not logged"
DEG_LINES=$(grep -c "DEGRADED" "$LOG")
run_wd "rbx1"   # still degraded: no repeat spam, no start
[ "$(grep -c "docker start" "$CALLS")" -eq 0 ] && ok "T3 still no start while degraded" || bad "T3 start while degraded"
[ "$(grep -c "DEGRADED" "$LOG")" -eq "$DEG_LINES" ] && ok "T3 no DEGRADED log spam" || bad "T3 DEGRADED logged again"

echo "== T4 sliding window expiry: old events pruned =="
setup
seed_events "rbx1.restart" 6 4000   # all older than 3600s
echo 2 > "$ST/rbx1.norun"
run_wd "rbx1"
[ "$(grep -c 'docker start wd-rbx1' "$CALLS")" -eq 1 ] && ok "T4 start allowed after window expiry" || bad "T4 start blocked by stale events"
[ -f "$ST/rbx1.degraded" ] && bad "T4 degraded" || ok "T4 not degraded"

echo "== T5 helper crash loop -> DEGRADED, no more restarts =="
setup
echo "wd-rbx1" > "$T/docker_ps"
B=$(inst_base rbx1); touch "$B/booted" "$B/live.png"
touch "$B/screencap-loop.sh" "$B/input-daemon.sh" "$B/roblox-keepalive.sh"
echo "screencap-loop.sh" > "$T/pgrep_dead"
seed_events "rbx1.helper-screencap_loop_sh" 8 60
run_wd "rbx1"
grep -q "nohup waydroid shell sh /data/media/0/jerusalem/screencap-loop.sh" "$CALLS" \
  && bad "T5 helper restarted past budget" || ok "T5 no helper restart past budget"
[ -f "$ST/rbx1.degraded" ] && ok "T5 degraded marker set" || bad "T5 degraded marker missing"
grep -q "DEGRADED: screencap-loop.sh" "$LOG" && ok "T5 DEGRADED names the helper" || bad "T5 DEGRADED helper name missing"

echo "== T6 degraded auto-clear when all green =="
setup
echo "wd-rbx1" > "$T/docker_ps"
B=$(inst_base rbx1); touch "$B/booted" "$B/live.png"
: > "$ST/rbx1.degraded"
run_wd "rbx1"
[ -f "$ST/rbx1.degraded" ] && bad "T6 degraded not cleared" || ok "T6 degraded cleared"
grep -q "recovered from DEGRADED" "$LOG" && ok "T6 recovery logged" || bad "T6 recovery not logged"
ls "$ST"/rbx1.*.ev >/dev/null 2>&1 && bad "T6 event files not cleaned" || ok "T6 event files cleaned"

echo "== T7 stuck boot (>15m, no marker) -> docker restart =="
setup
echo "wd-rbx1" > "$T/docker_ps"
inst_base rbx1 >/dev/null   # no booted marker
date -u -d '20 minutes ago' +%FT%TZ > "$T/docker_started_at"
run_wd "rbx1"
grep -q "docker restart" "$CALLS" && bad "T7 restart on strike 1" || ok "T7 no restart on strike 1"
run_wd "rbx1"
[ "$(grep -c 'docker restart wd-rbx1' "$CALLS")" -eq 1 ] && ok "T7 one docker restart on strike 2" || bad "T7 expected one docker restart"
[ "$(cat "$ST/rbx1.restart.ev" 2>/dev/null | wc -l)" -eq 1 ] && ok "T7 restart counted in shared budget" || bad "T7 restart not counted"

echo "== T8 stale stream (>30s) -> loop restart =="
setup
echo "wd-rbx1" > "$T/docker_ps"
B=$(inst_base rbx1); touch "$B/booted"
touch "$B/screencap-loop.sh" "$B/input-daemon.sh" "$B/roblox-keepalive.sh"
touch -d '5 minutes ago' "$B/live.png"
run_wd "rbx1"
grep -q "docker exec wd-rbx1 pkill -f screencap-loop.sh" "$CALLS" && ok "T8 stale loop killed" || bad "T8 stale loop not killed"
grep -q "nohup waydroid shell sh /data/media/0/jerusalem/screencap-loop.sh" "$CALLS" && ok "T8 loop restarted" || bad "T8 loop not restarted"

echo "== T9 corrupt event file tolerated =="
setup
printf 'garbage\n\nnotanumber\n%s\n%s\n' "$(date +%s)" "$(date +%s)" > "$ST/rbx1.restart.ev"
echo 2 > "$ST/rbx1.norun"
run_wd "rbx1"
[ "$(grep -c 'docker start wd-rbx1' "$CALLS")" -eq 1 ] && ok "T9 garbage ignored, start proceeds" || bad "T9 blocked by garbage events"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
