#!/bin/bash
# test_watchdog_lock.sh — unit tests for df-watchdog.sh v11 (single-instance lock).
# Runs the real watchdog against fake docker/df/curl/systemctl + temp dirs.
# Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; ST="$T/st"; VOL="$T/vol"; LOG="$T/watchdog.log"; CALLS="$T/calls.log"
MEMINFO="$T/meminfo"; DMESG="$T/dmesg"
mkdir -p "$BIN" "$ST" "$VOL"
export T CALLS   # the fake docker reads scenario files + call log from these

# ---- fake docker: healthy fleet (container up, booted, helpers alive) ----
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
      pgrep) exit 0 ;;  # helpers alive
      waydroid)
        case "$*" in
          *getprop*) echo 1; exit 0 ;;
          *dumpsys*) echo "    versionName=2.740.931"; exit 0 ;;
        esac
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
EOF
chmod +x "$BIN/docker"

# ---- fake df (70% used: no disk action) ----------------------------------
cat > "$BIN/df" <<'EOF'
#!/bin/bash
echo "Filesystem 1K-blocks Used Available Use% Mounted on"
echo "/dev/sda1 100000000 70000000 30000000 70% /var/lib/docker"
EOF
chmod +x "$BIN/df"

# ---- fake curl: $T/curl_code, optional $T/curl_sleep ----------------------
cat > "$BIN/curl" <<'EOF'
#!/bin/bash
s=$(cat "$T/curl_sleep" 2>/dev/null || echo 0)
[ "$s" -gt 0 ] 2>/dev/null && sleep "$s"
cat "$T/curl_code" 2>/dev/null || echo 200
exit 0
EOF
chmod +x "$BIN/curl"
cat > "$BIN/systemctl" <<'EOF'
#!/bin/bash
echo "systemctl $*" >> "$CALLS"
exit 0
EOF
chmod +x "$BIN/systemctl"

# ---- healthy host scenario ----------------------------------------------
setup() {
  rm -rf "$ST" "$VOL" "$T/lock"; mkdir -p "$ST" "$VOL"
  : > "$LOG"; : > "$CALLS"
  echo "wd-rbx1" > "$T/docker_ps"
  date -u +%FT%TZ > "$T/docker_started_at"
  echo 200 > "$T/curl_code"; echo 0 > "$T/curl_sleep"
  printf 'MemTotal:       32590000 kB\nMemAvailable:  20000000 kB\n' > "$MEMINFO"
  printf 'quiet\n' > "$DMESG"
  local d="$VOL/wd-rbx1-data/_data/data/media/0/jerusalem"
  mkdir -p "$d"; touch "$d/booted" "$d/live.png"
}
run_wd() { # run_wd [extra env]: run the watchdog once with its own lock dir
  # (LOCKDIR_OVERRIDE lets a single test point the lock elsewhere)
  DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="rbx1" DF_VOLBASE="$VOL" \
    DF_LOCKDIR="${LOCKDIR_OVERRIDE:-$T/lock}" DF_PROC_MEMINFO="$MEMINFO" DF_DMESG="$DMESG" \
    DF_LOG_GLOB="$T/notrim/*-json.log" \
    PATH="$BIN:/usr/bin:/bin" "$@" bash "$SRC" >/dev/null 2>&1
}

echo "== bash -n =="
bash -n "$SRC" && ok "syntax" || bad "syntax"

echo "== T1 healthy run: lock acquired, checks run, lock released =="
setup
run_wd
[ $? -eq 0 ] && ok "exit 0" || bad "non-zero exit"
grep -q "^docker ps" "$CALLS" && ok "checks ran (docker ps called)" || bad "checks did not run"
[ ! -e "$T/lock" ] && ok "lock released after run" || bad "lock dir left behind"
grep -q "stale lock" "$LOG" && bad "spurious stale-lock WARN" || ok "no stale-lock WARN"

echo "== T2 concurrent run on a fresh lock: silent exit, no checks =="
setup
mkdir -p "$T/lock"; echo 99999 > "$T/lock/pid"   # foreign holder, fresh
: > "$LOG"; : > "$CALLS"
run_wd
[ $? -eq 0 ] && ok "exit 0" || bad "non-zero exit"
[ ! -s "$CALLS" ] && ok "no checks ran (docker untouched)" || bad "checks ran despite lock"
[ ! -s "$LOG" ] && ok "silent (nothing logged)" || bad "logged despite fresh lock"
[ -d "$T/lock" ] && [ "$(cat "$T/lock/pid")" = "99999" ] \
  && ok "foreign lock untouched" || bad "foreign lock disturbed"

echo "== T3 stale lock: takeover with one WARN, checks run =="
setup
mkdir -p "$T/lock"; echo 99999 > "$T/lock/pid"
touch -d '10 minutes ago' "$T/lock"
: > "$LOG"; : > "$CALLS"
DF_LOCK_STALE_SECS=60 run_wd
[ $? -eq 0 ] && ok "exit 0" || bad "non-zero exit"
[ "$(grep -c "stale lock" "$LOG")" = "1" ] \
  && ok "exactly one stale-lock WARN" || bad "stale WARN count wrong"
grep -q "^docker ps" "$CALLS" && ok "checks ran after takeover" || bad "no checks after takeover"
[ ! -e "$T/lock" ] && ok "lock released after run" || bad "lock dir left behind"

echo "== T4 broken lock dir: WARN + skip, never silent-disable =="
setup
: > "$LOG"; : > "$CALLS"
LOCKDIR_OVERRIDE="$T/no-such-parent/lock" run_wd
[ $? -eq 0 ] && ok "exit 0" || bad "non-zero exit"
grep -q "cannot create lock dir" "$LOG" \
  && ok "broken lock dir WARN logged" || bad "broken lock dir silent"
[ ! -s "$CALLS" ] && ok "no checks ran" || bad "checks ran without lock"

echo "== T5 backgrounded check_one must not release the lock early =="
setup
LOG1="$T/log1"; CALLS1="$T/calls1"; : > "$LOG1"; : > "$CALLS1"
echo 3 > "$T/curl_sleep"   # slow manager self-check keeps run1 in flight
DF_ST="$ST" DF_LOG="$LOG1" DF_INSTANCES="rbx1" DF_VOLBASE="$VOL" \
  DF_LOCKDIR="$T/lock" DF_PROC_MEMINFO="$MEMINFO" DF_DMESG="$DMESG" \
  DF_LOG_GLOB="$T/notrim/*-json.log" CALLS="$CALLS1" \
  PATH="$BIN:/usr/bin:/bin" bash "$SRC" >/dev/null 2>&1 &
R1=$!
sleep 1
if [ -d "$T/lock" ] && [ "$(cat "$T/lock/pid" 2>/dev/null)" = "$R1" ]; then
  ok "lock held mid-run with run1 pid"
else
  bad "lock missing/wrong pid mid-run (subshell released it early?)"
fi
LOG2="$T/log2"; CALLS2="$T/calls2"; : > "$LOG2"; : > "$CALLS2"
DF_ST="$ST" DF_LOG="$LOG2" DF_INSTANCES="rbx1" DF_VOLBASE="$VOL" \
  DF_LOCKDIR="$T/lock" DF_PROC_MEMINFO="$MEMINFO" DF_DMESG="$DMESG" \
  DF_LOG_GLOB="$T/notrim/*-json.log" CALLS="$CALLS2" \
  PATH="$BIN:/usr/bin:/bin" bash "$SRC" >/dev/null 2>&1
[ $? -eq 0 ] && ok "run2 exit 0" || bad "run2 non-zero exit"
[ ! -s "$CALLS2" ] && ok "run2 ran no checks" || bad "run2 ran checks concurrently"
[ ! -s "$LOG2" ] && ok "run2 silent" || bad "run2 logged"
wait $R1
[ ! -e "$T/lock" ] && ok "lock released when run1 finished" || bad "lock left behind"
echo 0 > "$T/curl_sleep"

echo "== T6 pid-checked release: hung run cannot delete a newer holder's lock =="
setup
echo 10 > "$T/curl_sleep"
DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="rbx1" DF_VOLBASE="$VOL" \
  DF_LOCKDIR="$T/lock" DF_PROC_MEMINFO="$MEMINFO" DF_DMESG="$DMESG" \
  DF_LOG_GLOB="$T/notrim/*-json.log" \
  PATH="$BIN:/usr/bin:/bin" bash "$SRC" >/dev/null 2>&1 &
R1=$!
sleep 0.5
kill -0 $R1 2>/dev/null && [ -d "$T/lock" ] || bad "run1 did not start/lock"
# simulate a stale-takeover by a newer run while run1 is hung
rm -rf "$T/lock"; mkdir "$T/lock"; echo 424242 > "$T/lock/pid"
kill -TERM $R1 2>/dev/null
wait $R1 2>/dev/null
if [ -d "$T/lock" ] && [ "$(cat "$T/lock/pid" 2>/dev/null)" = "424242" ]; then
  ok "newer holder's lock survived the hung run's exit"
else
  bad "hung run deleted the newer holder's lock"
fi
echo 0 > "$T/curl_sleep"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
