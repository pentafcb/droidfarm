#!/bin/bash
# test_watchdog_liveness.sh — unit tests for the df-watchdog liveness heartbeat.
# Runs the REAL watchdog against fake docker/curl/systemctl + temp state/log
# dirs, and asserts the $ST/heartbeat mtime contract: touched on every executed
# run (after the v11 lock), never touched by a silent-exit second run.
# Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
echo "5000 0" > "$T/uptime"  # pin high uptime: v14 boot recovery stays out
BIN="$T/bin"; ST="$T/st"; VOL="$T/vol"; LOG="$T/watchdog.log"; CALLS="$T/calls.log"
mkdir -p "$BIN" "$ST" "$VOL"
export T CALLS

# ---- fake docker (healthy instance, session alive) -------------------------
cat > "$BIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
sub=$1; shift
case "$sub" in
  ps)      cat "$T/docker_ps" 2>/dev/null ;;
  inspect) cat "$T/docker_started_at" 2>/dev/null ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep) exit 0 ;;                       # helpers alive
      waydroid) case "$*" in *getprop*) echo 1; exit 0;; esac; exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
EOF
chmod +x "$BIN/docker"

# ---- fake curl + systemctl (v5 manager self-check stays hermetic) ----------
cat > "$BIN/curl" <<'EOF'
#!/bin/bash
cat "$T/curl_code" 2>/dev/null || echo 200
exit 0
EOF
chmod +x "$BIN/curl"
cat > "$BIN/systemctl" <<'EOF'
#!/bin/bash
case "$1" in
  is-active) exit 0 ;;
  start|restart) exit 0 ;;
esac
exit 0
EOF
chmod +x "$BIN/systemctl"

setup() {
  rm -rf "$ST" "$VOL" "$T/lock"; mkdir -p "$ST" "$VOL"
  : > "$CALLS"; : > "$LOG"
  echo "wd-rbx1" > "$T/docker_ps"
  date -u +%FT%TZ > "$T/docker_started_at"
  mkdir -p "$VOL/wd-rbx1-data/_data/data/media/0/jerusalem"
  B="$VOL/wd-rbx1-data/_data/data/media/0/jerusalem"
  touch "$B/booted" "$B/live.png"
}
run_wd() { # run_wd <instances>: run the watchdog once
  DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="$1" DF_VOLBASE="$VOL" \
    DF_LOCKDIR="$T/lock" DF_PROC_UPTIME="$T/uptime" \
    PATH="$BIN:/usr/bin:/bin" bash "$SRC" >/dev/null 2>&1
}
hb_age() { # seconds since heartbeat mtime; prints "missing" when absent
  [ -f "$ST/heartbeat" ] || { echo missing; return; }
  echo $(( $(date +%s) - $(stat -c %Y "$ST/heartbeat") ))
}

echo "== bash -n =="
bash -n "$SRC" && ok "syntax" || bad "syntax"

echo "== T1 fresh run touches the heartbeat =="
setup
run_wd "rbx1"
[ -f "$ST/heartbeat" ] || bad "T1 heartbeat file missing after run"
a=$(hb_age)
[ "$a" != "missing" ] && [ "$a" -lt 20 ] && ok "T1 heartbeat fresh (${a}s)" \
  || bad "T1 heartbeat stale/missing: $a"

echo "== T2 second run refreshes the mtime =="
touch -d '2001-01-01 00:00:00' "$ST/heartbeat"
run_wd "rbx1"
a=$(hb_age)
[ "$a" != "missing" ] && [ "$a" -lt 20 ] && ok "T2 heartbeat refreshed (${a}s)" \
  || bad "T2 heartbeat not refreshed: $a"

echo "== T3 foreign lock: silent exit, heartbeat untouched, no docker =="
touch -d '2001-01-01 00:00:00' "$ST/heartbeat"
mkdir -p "$T/lock"; echo 999999 > "$T/lock/pid"   # foreign lock held
: > "$CALLS"
run_wd "rbx1"
a=$(hb_age)
[ "$a" = "missing" ] || [ "$a" -gt 100000 ] && ok "T3 heartbeat untouched by locked-out run" \
  || bad "T3 heartbeat was touched despite foreign lock: $a"
grep -q "docker" "$CALLS" && bad "T3 docker called despite foreign lock" \
  || ok "T3 no docker calls on foreign lock"

echo "== T4 heartbeat is an empty regular file =="
[ -f "$ST/heartbeat" ] && [ ! -s "$ST/heartbeat" ] && ok "T4 empty regular file" \
  || bad "T4 heartbeat not an empty regular file"

echo "== T5 broken lock dir: WARN + skip, heartbeat untouched =="
touch -d '2001-01-01 00:00:00' "$ST/heartbeat"
rm -rf "$T/lock"; echo x > "$T/lock"   # lock path is a file: mkdir fails
run_wd "rbx1"
grep -q "cannot create lock dir" "$LOG" && ok "T5 broken-lock WARN logged" \
  || bad "T5 broken-lock WARN missing"
a=$(hb_age)
[ "$a" = "missing" ] || [ "$a" -gt 100000 ] && ok "T5 heartbeat untouched on broken lock" \
  || bad "T5 heartbeat touched despite broken lock: $a"

echo "== T6 DF_ST honored (no write to the default /tmp/df-watchdog) =="
rm -f /tmp/df-watchdog/heartbeat   # default must stay clean even in tests
setup
run_wd "rbx1"
[ -f "$ST/heartbeat" ] && ok "T6 heartbeat under DF_ST" || bad "T6 heartbeat not under DF_ST"
[ -f /tmp/df-watchdog/heartbeat ] && bad "T6 wrote to the default ST path" \
  || ok "T6 default ST untouched"

echo "== T7 healthy run still no-ops (no recovery actions) =="
setup
: > "$CALLS"
run_wd "rbx1"
grep -q "docker start\|docker restart\|nohup" "$CALLS" && bad "T7 unexpected recovery action" \
  || ok "T7 no recovery action on healthy run"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
