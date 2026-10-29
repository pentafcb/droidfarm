#!/bin/bash
# test_boot_recovery.sh — unit tests for df-watchdog.sh v14 (section 0):
#   host-boot fleet recovery: binderfs pool restore + staggered starts.
# Runs the real watchdog against fake `docker`/`curl`/`systemctl`/`mount`,
# a fake /proc/uptime and a fake binderfs (FIFO binder-control with a
# background name-logger, so every binder-control write is observable).
# Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'kill "$READER" 2>/dev/null; rm -rf "$T"' EXIT
BIN="$T/bin"; ST="$T/st"; VOL="$T/vol"; LOG="$T/watchdog.log"; CALLS="$T/calls.log"
UPTIME="$T/uptime"; BFS="$T/binderfs"
mkdir -p "$BIN" "$ST" "$VOL" "$BFS"
export T CALLS READER

# ---- fake docker (stateful: $T/docker_down lists stopped containers) -----
cat > "$BIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
sub=$1; shift
case "$sub" in
  ps)
    if [ "$1" = "-a" ]; then
      for c in wd-rbx1 wd-rbx2 wd-rbx3 wd-rbx4 wd-rbx5; do
        if grep -qx "$c" "$T/docker_down" 2>/dev/null; then echo "$c|exited"; else echo "$c|running"; fi
      done
    else
      for c in wd-rbx1 wd-rbx2 wd-rbx3 wd-rbx4 wd-rbx5; do
        grep -qx "$c" "$T/docker_down" 2>/dev/null || echo "$c"
      done
    fi ;;
  inspect) cat "$T/docker_started_at" 2>/dev/null ;;
  start)
    [ -f "$T/start_fail" ] && exit 1
    grep -vx "$1" "$T/docker_down" 2>/dev/null > "$T/docker_down.new"
    mv "$T/docker_down.new" "$T/docker_down" 2>/dev/null
    exit 0 ;;
  restart) exit 0 ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep) exit 0 ;;        # helpers alive
      waydroid) exit 1 ;;    # session probe / dumpsys fail -> silent skips
      *) exit 0 ;;
    esac ;;
  image|builder) exit 0 ;;
esac
exit 0
EOF
chmod +x "$BIN/docker"

# ---- fake curl (healthy manager), systemctl, mount (always fails) --------
printf '#!/bin/bash\necho 200; exit 0\n' > "$BIN/curl"
printf '#!/bin/bash\necho "systemctl $*" >> "$CALLS"\nexit 0\n' > "$BIN/systemctl"
printf '#!/bin/bash\nexit 1\n' > "$BIN/mount"
chmod +x "$BIN/curl" "$BIN/systemctl" "$BIN/mount"

# ---- fake binderfs: FIFO binder-control + background name logger ---------
# NOTE: the logger holds the FIFO open O_RDWR (exec 3<>...). A plain
# `done < fifo` reader sees EOF and exits the moment no writer holds the
# fifo (classic race: the slow pool loop's inter-write gaps look like
# EOF), after which the next `> fifo` write blocks forever. O_RDWR keeps
# a writer present, so reads block instead of EOF.
binder_fifo() { # binder_fifo: (re)create FIFO binder-control + logger
  kill "$READER" 2>/dev/null
  rm -rf "$BFS"; mkdir -p "$BFS"
  mkfifo "$BFS/binder-control"
  : > "$BFS/names.log"
  ( exec 3<>"$BFS/binder-control"
    while IFS= read -r -u 3 line; do printf '%s\n' "$line" >> "$BFS/names.log"; done ) &
  READER=$!
  export READER
}

# ---- scenario setup -------------------------------------------------------
setup() { # setup <uptime_secs>: fresh state, all 5 containers stopped
  kill "$READER" 2>/dev/null; READER=""
  rm -rf "$ST" "$VOL" "$BFS"; mkdir -p "$ST" "$VOL" "$BFS"
  : > "$CALLS"; : > "$LOG"
  rm -f "$T/start_fail" "$T/docker_down"
  printf 'wd-rbx1\nwd-rbx2\nwd-rbx3\nwd-rbx4\nwd-rbx5\n' > "$T/docker_down"
  date -u +%FT%TZ > "$T/docker_started_at"   # recent start: no stuck-boot
  echo "${1:-100} 0" > "$UPTIME"
  binder_fifo
}
run_wd() { # run_wd [extra VAR=val ...]: run the watchdog once
  # NOTE: extra vars go through `env`, not the assignment prefix: a bare
  # `"$@"` inside `VAR=... "$@" ... cmd` is parsed as the COMMAND (bash
  # decides assignments before expanding "$@" -- 127 "command not found").
  # "$@" comes LAST so overrides win over the defaults above.
  env DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="rbx1 rbx2 rbx3 rbx4 rbx5" \
    DF_VOLBASE="$VOL" DF_BINDERFS="$BFS" DF_PROC_UPTIME="$UPTIME" \
    DF_LOCKDIR="$T/lock" DF_LOG_GLOB="$T/notrim/*" \
    PATH="$BIN:/usr/bin:/bin" "$@" bash "$SRC" >/dev/null 2>&1
}
starts() { grep -c '^docker start ' "$CALLS" 2>/dev/null || true; }

echo "== bash -n =="
bash -n "$SRC" && ok "syntax" || bad "syntax"

echo "== R1 fresh boot: pool restored, starts staggered =="
setup 100
run_wd DF_BOOT_MAX_STARTS=2
[ "$(starts)" = "2" ] && ok "cycle 1 starts exactly 2 (staggered, not 5)" || bad "cycle 1 starts: $(starts), want 2"
grep -q '^docker start wd-rbx1$' "$CALLS" && grep -q '^docker start wd-rbx2$' "$CALLS" \
  && ok "cycle 1 starts rbx1,rbx2 in order" || bad "cycle 1 start order"
[ "$(wc -l < "$BFS/names.log")" = "64" ] && ok "binder pool: 64 binder-control writes" || bad "binder-control writes: $(wc -l < "$BFS/names.log")"
head -1 "$BFS/names.log" | grep -qx "binder" && tail -1 "$BFS/names.log" | grep -qx "binder63" \
  && ok "binder-control write order binder..binder63" || bad "binder-control write order"
[ ! -f "$ST/boot.done" ] && ok "no sentinel while queue remains" || bad "sentinel written too early"
grep -q "container down -> docker start" "$LOG" && bad "check_one started containers during recovery" \
  || ok "check_one issued no starts during recovery"

echo "== R2 queue drains over cycles, sentinel on completion =="
run_wd DF_BOOT_MAX_STARTS=2
[ "$(starts)" = "4" ] && ok "cycle 2 total starts 4" || bad "cycle 2 total starts: $(starts)"
[ ! -f "$ST/boot.done" ] && ok "still no sentinel after cycle 2" || bad "sentinel too early (cycle 2)"
run_wd DF_BOOT_MAX_STARTS=2
[ "$(starts)" = "5" ] && ok "cycle 3 total starts 5" || bad "cycle 3 total starts: $(starts)"
[ -f "$ST/boot.done" ] && ok "sentinel written when queue drained" || bad "no sentinel after drain"
grep -q "boot recovery complete" "$LOG" && ok "completion logged" || bad "no completion log"

echo "== R3 after sentinel: normal 2-strike check_one path resumes =="
printf 'wd-rbx5\n' > "$T/docker_down"   # rbx5 "stops" again post-recovery
: > "$CALLS"
run_wd
[ "$(starts)" = "0" ] && ok "strike 1: no start yet" || bad "strike 1 started"
run_wd
[ "$(starts)" = "1" ] && ok "strike 2: check_one starts it" || bad "strike 2 starts: $(starts)"
grep -q "rbx5 container down -> docker start" "$LOG" && ok "normal-path log line" || bad "no normal-path log"

echo "== R4 missed window: sentinel, no starts, normal checks own it =="
setup 5000
run_wd
[ -f "$ST/boot.done" ] && ok "sentinel written on missed window" || bad "no sentinel on missed window"
[ "$(starts)" = "0" ] && ok "no starts on missed window" || bad "starts on missed window: $(starts)"
grep -q "boot window missed" "$LOG" && ok "missed-window logged" || bad "no missed-window log"
[ ! -s "$BFS/names.log" ] && ok "no pool writes on missed window" || bad "pool writes on missed window"

echo "== R5 sentinel present from the start: section 0 no-op =="
setup 100
touch "$ST/boot.done"
run_wd
[ "$(starts)" = "0" ] && ok "no boot-recovery starts (strike 1 only)" || bad "starts with sentinel: $(starts)"
grep -q "boot recovery" "$LOG" && bad "section 0 ran with sentinel" || ok "section 0 silent with sentinel"

echo "== R6 unreadable uptime: fail open, no sentinel =="
setup 100
run_wd DF_PROC_UPTIME="$T/no-such-uptime"
[ ! -f "$ST/boot.done" ] && ok "no sentinel on unreadable uptime" || bad "sentinel on unreadable uptime"
grep -q "fresh host boot" "$LOG" && bad "recovery ran on unreadable uptime" || ok "recovery skipped (fail open)"
run_wd DF_PROC_UPTIME="$T/no-such-uptime"
[ "$(starts)" = "5" ] && ok "check_one 2-strike path starts all 5 over 2 cycles" || bad "fail-open starts: $(starts)"

echo "== R7 empty docker stub dir: rmdir + recreate =="
setup 100
mkdir -p "$BFS/binder7"
run_wd DF_BOOT_MAX_STARTS=5
[ ! -d "$BFS/binder7" ] && ok "empty stub dir removed" || bad "stub dir still present"
grep -qx "binder7" "$BFS/names.log" && ok "binder7 recreated via binder-control" || bad "binder7 not recreated"
grep -q "removed empty docker bind-stub dir binder7" "$LOG" && ok "stub removal logged" || bad "no stub-removal log"

echo "== R8 non-empty stub dir: ALERT, start paused, dir kept =="
setup 100
mkdir -p "$BFS/binder9"; touch "$BFS/binder9/junk"
run_wd
grep -q "ALERT boot recovery: $BFS/binder9 is a non-empty directory" "$LOG" \
  && ok "ALERT names the stub dir" || bad "no stub ALERT"
[ -d "$BFS/binder9" ] && ok "non-empty stub dir NOT deleted" || bad "non-empty stub dir deleted"
[ "$(starts)" = "0" ] && ok "fleet start paused" || bad "starts despite stub: $(starts)"
[ ! -f "$ST/boot.done" ] && ok "no sentinel while blocked" || bad "sentinel while blocked"
: > "$CALLS"; : > "$LOG"
run_wd
grep -q "ALERT boot recovery" "$LOG" && bad "ALERT repeated every cycle" || ok "ALERT once per boot"

echo "== R9 degraded instances never started by recovery =="
setup 100
touch "$ST/rbx3.degraded"
run_wd DF_BOOT_MAX_STARTS=5
grep -q '^docker start wd-rbx3$' "$CALLS" && bad "degraded rbx3 started" || ok "degraded rbx3 skipped"
[ "$(starts)" = "4" ] && ok "other 4 started" || bad "starts: $(starts), want 4"

echo "== R10 start failures: budget then DEGRADED, queue still drains =="
setup 100
touch "$T/start_fail"
run_wd DF_BOOT_MAX_STARTS=2 DF_BOOT_ATTEMPTS=1
[ "$(starts)" = "2" ] && ok "cycle 1: 2 attempts" || bad "cycle 1 attempts: $(starts)"
run_wd DF_BOOT_MAX_STARTS=2 DF_BOOT_ATTEMPTS=1
[ -f "$ST/rbx1.degraded" ] && [ -f "$ST/rbx2.degraded" ] \
  && ok "cycle 2: rbx1,rbx2 DEGRADED after budget" || bad "no DEGRADED after budget"
head -1 "$ST/rbx1.degraded" | grep -q "failed to start" \
  && ok "degraded reason on first line" || bad "degraded reason missing"
grep -q "DEGRADED: failed to start" "$LOG" && ok "DEGRADED logged" || bad "no DEGRADED log"
run_wd DF_BOOT_MAX_STARTS=2 DF_BOOT_ATTEMPTS=1
run_wd DF_BOOT_MAX_STARTS=2 DF_BOOT_ATTEMPTS=1
run_wd DF_BOOT_MAX_STARTS=2 DF_BOOT_ATTEMPTS=1
run_wd DF_BOOT_MAX_STARTS=2 DF_BOOT_ATTEMPTS=1
[ -f "$ST/boot.done" ] && ok "sentinel written once queue drained (all degraded)" || bad "no sentinel after drain"
[ "$(starts)" = "5" ] && ok "exactly 1 attempt per instance (no retry after DEGRADED)" || bad "start count: $(starts), want 5"

echo "== R11 binderfs missing: WARN, no starts, no sentinel, retry next cycle =="
setup 100
rm -rf "$BFS"; mkdir -p "$BFS"   # no binder-control; fake mount fails
kill "$READER" 2>/dev/null; READER=""
run_wd
grep -q "WARN boot recovery: binderfs unavailable" "$LOG" && ok "unavailable WARN logged" || bad "no WARN"
[ "$(starts)" = "0" ] && ok "no starts without binderfs" || bad "starts without binderfs"
[ ! -f "$ST/boot.done" ] && ok "no sentinel (retries next cycle)" || bad "sentinel without binderfs"

echo "== R12 garbage env: knobs sanitize, no herd =="
setup 100
run_wd DF_BOOT_MAX_STARTS="abc" DF_BOOT_WINDOW_S="" DF_BOOT_ATTEMPTS="0"
[ "$(starts)" = "3" ] && ok "garbage max-starts sanitized to 3" || bad "starts with garbage env: $(starts)"

echo "== R13 pool already complete: no binder-control writes =="
setup 100
kill "$READER" 2>/dev/null; READER=""
rm -f "$BFS/binder-control"
touch "$BFS/binder-control"
for i in $(seq 0 63); do n="binder"; [ "$i" -gt 0 ] && n="binder$i"; touch "$BFS/$n"; done
: > "$BFS/names.log"
( while IFS= read -r line; do printf '%s\n' "$line" >> "$BFS/names.log"; done < "$BFS/binder-control" ) &
READER=$!
run_wd DF_BOOT_MAX_STARTS=5
[ ! -s "$BFS/names.log" ] && ok "no writes when pool complete" || bad "writes on complete pool: $(cat "$BFS/names.log")"
grep -q "binder pool at 64/64" "$LOG" && ok "pool count logged 64/64" || bad "no 64/64 log"

echo "== R14 source contracts =="
grep -q "if boot_recovery_active; then" "$SRC" \
  && ok "check_one down-branch guards on boot_recovery_active" || bad "guard missing in check_one"
grep -q "^boot_recovery$" "$SRC" \
  && ok "boot_recovery called in main flow" || bad "boot_recovery not called"
grep -q 'rmdir "$BINDERFS/$name"' "$SRC" \
  && ok "stub cleanup uses rmdir (empty-only)" || bad "no rmdir stub cleanup"
! grep -q "rm -rf \"\$BINDERFS" "$SRC" \
  && ok "no recursive delete of binderfs" || bad "dangerous binderfs delete found"

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
