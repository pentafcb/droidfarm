#!/bin/bash
# test_watchdog_v9.sh — unit tests for df-watchdog.sh v9 (waydroid session
# death detection + auto-restart, check_one section 2b):
#   S1  session alive -> section passes through, no action, grace ts cleared
#   S2  session dead, first sighting -> detached `waydroid session start`,
#       grace ts written, early return (helper/stream checks skipped)
#   S3  session still dead inside 180s grace -> wait, no new start, no
#       budget burn
#   S4  grace expired -> new start attempt, budget counts
#   S5  >4 starts/hour -> DEGRADED marker (reason on first line), no start
#   S6  rerun while degraded -> no log spam, still no start
#   S7  no booted marker (first boot) -> probe skipped entirely
#   S8  session start command fails -> FAILED logged, no grace ts, counted
#   S9  session recovers -> grace ts cleared
#   S10 contract: section-6 auto-clear resets the session budget too
#   S11 contract: probe uses the entrypoint's getprop idiom
#   S12 safety: the section never stops/kills/removes anything
# Runs the REAL log/note_event/cb_guard + the verbatim 2b block against a
# fake `docker`. Hermetic. Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; ST="$T/st"; VOL="$T/vol"; LOG="$T/watchdog.log"; CALLS="$T/calls.log"
mkdir -p "$BIN" "$ST" "$VOL"
export T CALLS

# ---- fake docker -----------------------------------------------------------
cat > "$BIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
sub=$1; shift
case "$sub" in
  exec)
    if [ "$1" = "-d" ]; then
      shift
      echo "DETACHED: $*" >> "$CALLS"
      [ -f "$T/sessionstart_fail" ] && exit 1
      exit 0
    fi
    cname=$1; shift
    case "$1" in
      waydroid)
        case "$*" in
          *getprop*) [ -f "$T/session_dead" ] && exit 1; echo 1; exit 0 ;;
        esac
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
EOF
chmod +x "$BIN/docker"

# ---- build the harness: real helpers + verbatim 2b block --------------------
{
  echo "ST=\"$ST\""
  echo "LOG=\"$LOG\""
  sed -n '/^log() {/,/^}/p' "$SRC"
  sed -n '/^note_event() {/,/^}/p' "$SRC"
  sed -n '/^cb_guard() {/,/^}/p' "$SRC"
  sed -n '/^# --- v13: bounded docker calls ---$/,/^# --- end v13 ---$/p' "$SRC"
  echo 'check2b() {'
  echo '  local i="rbx1" c="wd-rbx1"'
  echo "  local base=\"$VOL/wd-rbx1-data/_data/data/media/0/jerusalem\""
  echo '  local healthy=1 n'
  sed -n '/# --- V9-2B-START ---/,/# --- V9-2B-END ---/p' "$SRC"
  echo '  echo "CONTINUED healthy=$healthy"'
  echo '}'
  echo 'check2b'
} > "$T/check2b.sh"

# ---- scenario setup ---------------------------------------------------------
setup() { # setup [noboot]: fresh state; booted marker unless "noboot"
  rm -rf "$ST" "$VOL"; mkdir -p "$ST" "$VOL"
  : > "$CALLS"; : > "$LOG"
  rm -f "$T/session_dead" "$T/sessionstart_fail"
  if [ "${1:-}" != "noboot" ]; then
    mkdir -p "$VOL/wd-rbx1-data/_data/data/media/0/jerusalem"
    touch "$VOL/wd-rbx1-data/_data/data/media/0/jerusalem/booted"
  fi
}
run_2b() { PATH="$BIN:/usr/bin:/bin" bash "$T/check2b.sh" 2>/dev/null; }
seed_events() { # seed_events <key> <count> <age_secs>
  local f="$ST/$1.ev" n=$2 age=$3 now i
  now=$(date +%s); : > "$f"
  for i in $(seq 1 "$n"); do echo $((now - age)) >> "$f"; done
}
detached_starts() { grep -c "^DETACHED: wd-rbx1 waydroid session start$" "$CALLS" 2>/dev/null; }
degraded_lines() { grep -c "DEGRADED: waydroid session" "$LOG" 2>/dev/null; }

echo "== bash -n =="
bash -n "$SRC" && ok "watchdog syntax" || bad "watchdog syntax"
grep -q "V9-2B-START" "$T/check2b.sh" && ok "2b block extracted" || bad "2b block missing"

echo "== S1: session alive -> pass through, no action =="
setup
OUT=$(run_2b)
echo "$OUT" | grep -q "^CONTINUED healthy=1$" && ok "S1 continued, healthy=1" || bad "S1 out=[$OUT]"
grep -q "waydroid session" "$LOG" && bad "S1 logged about session" || ok "S1 log silent"
[ "$(detached_starts)" = "0" ] && ok "S1 no session start" || bad "S1 started a session"
[ ! -f "$ST/rbx1.sessionstart.ts" ] && ok "S1 no grace ts" || bad "S1 grace ts written"

echo "== S2: session dead, first sighting -> detached start + early return =="
setup
touch "$T/session_dead"
OUT=$(run_2b)
echo "$OUT" | grep -q "^CONTINUED" && bad "S2 did not return early" || ok "S2 early return (helpers skipped)"
grep -q "rbx1 waydroid session dead -> session start (1/4 this hour)" "$LOG" \
  && ok "S2 dead->start logged with budget" || bad "S2 log: $(cat "$LOG")"
[ "$(detached_starts)" = "1" ] && ok "S2 one detached session start" || bad "S2 starts=$(detached_starts)"
[ -f "$ST/rbx1.sessionstart.ts" ] && ok "S2 grace ts written" || bad "S2 no grace ts"
[ ! -f "$ST/rbx1.degraded" ] && ok "S2 not degraded yet" || bad "S2 degraded too early"
grep -q "waydroid session start issued" "$LOG" && ok "S2 issued logged" || bad "S2 issued not logged"

echo "== S3: still dead inside 180s grace -> wait, no new start, no burn =="
echo $(( $(date +%s) - 60 )) > "$ST/rbx1.sessionstart.ts"
: > "$CALLS"
run_2b >/dev/null
grep -q "still down (restart issued 6" "$LOG" && ok "S3 waiting logged" || bad "S3 log: $(cat "$LOG")"
[ "$(detached_starts)" = "0" ] && ok "S3 no new start during grace" || bad "S3 started during grace"
[ "$(wc -l < "$ST/rbx1.session.ev")" = "1" ] && ok "S3 budget not burned" || bad "S3 event count grew"

echo "== S4: grace expired -> new attempt, budget counts =="
echo $(( $(date +%s) - 200 )) > "$ST/rbx1.sessionstart.ts"
: > "$CALLS"; : > "$LOG"
run_2b >/dev/null
[ "$(detached_starts)" = "1" ] && ok "S4 new start after grace" || bad "S4 no start"
grep -q "session start (2/4 this hour)" "$LOG" && ok "S4 budget at 2/4" || bad "S4 log: $(cat "$LOG")"

echo "== S5: >4 starts/hour -> DEGRADED, no start attempted =="
setup
touch "$T/session_dead"
seed_events "rbx1.session" 4 60
: > "$CALLS"; : > "$LOG"
run_2b >/dev/null
[ -f "$ST/rbx1.degraded" ] && ok "S5 degraded marker set" || bad "S5 no marker"
head -1 "$ST/rbx1.degraded" | grep -q "waydroid session restarted 5 times in last 3600s - auto-recovery paused" \
  && ok "S5 reason on first line" || bad "S5 reason: $(head -1 "$ST/rbx1.degraded" 2>/dev/null)"
[ "$(detached_starts)" = "0" ] && ok "S5 no start past budget" || bad "S5 started past budget"
[ "$(degraded_lines)" = "1" ] && ok "S5 one DEGRADED line" || bad "S5 lines=$(degraded_lines)"

echo "== S6: rerun while degraded -> no spam, still no start =="
: > "$CALLS"
run_2b >/dev/null
[ "$(degraded_lines)" = "1" ] && ok "S6 no DEGRADED spam" || bad "S6 lines=$(degraded_lines)"
[ "$(detached_starts)" = "0" ] && ok "S6 still no start" || bad "S6 started while degraded"

echo "== S7: no booted marker (first boot) -> probe skipped =="
setup noboot
touch "$T/session_dead"
: > "$CALLS"
OUT=$(run_2b)
echo "$OUT" | grep -q "^CONTINUED healthy=1$" && ok "S7 continued (stuck-boot owns first boot)" || bad "S7 out=[$OUT]"
grep -q "getprop" "$CALLS" && bad "S7 probed during first boot" || ok "S7 no probe during first boot"
grep -q "waydroid session" "$LOG" && bad "S7 logged about session" || ok "S7 log silent"

echo "== S8: session start command fails -> FAILED, no grace, counted =="
setup
touch "$T/session_dead" "$T/sessionstart_fail"
: > "$CALLS"; : > "$LOG"
run_2b >/dev/null
grep -q "rbx1 waydroid session start FAILED" "$LOG" && ok "S8 FAILED logged" || bad "S8 log: $(cat "$LOG")"
[ ! -f "$ST/rbx1.sessionstart.ts" ] && ok "S8 no grace ts on failure" || bad "S8 grace ts written on failure"
[ -f "$ST/rbx1.session.ev" ] && [ "$(wc -l < "$ST/rbx1.session.ev")" = "1" ] \
  && ok "S8 attempt counted" || bad "S8 event not counted"

echo "== S9: session recovers -> grace ts cleared =="
setup
echo $(( $(date +%s) - 60 )) > "$ST/rbx1.sessionstart.ts"
OUT=$(run_2b)
echo "$OUT" | grep -q "^CONTINUED healthy=1$" && ok "S9 continued" || bad "S9 out=[$OUT]"
[ ! -f "$ST/rbx1.sessionstart.ts" ] && ok "S9 grace ts cleared" || bad "S9 ts not cleared"

echo "== S10: section-6 auto-clear resets the session budget =="
grep -q 'rm -f "\$ST/\$i.degraded".*"\$ST/\$i.session.ev"' "$SRC" \
  && ok "S10 section-6 clears session.ev" || bad "S10 session.ev not in auto-clear"

echo "== S11: probe uses the entrypoint getprop idiom (now dw-bounded) =="
grep -q 'dw "\$DW_FAST" exec "\$c" waydroid shell getprop sys.boot_completed' "$SRC" \
  && ok "S11 probe idiom present (dw-bounded)" || bad "S11 probe idiom missing"

echo "== S12: section never stops/kills/removes anything =="
SEC="$T/section2b.txt"
sed -n '/# --- V9-2B-START ---/,/# --- V9-2B-END ---/p' "$SRC" > "$SEC"
grep -Eq "docker (stop|kill|rm|rmi)|volume prune|rm -rf /" "$SEC" \
  && bad "S12 destructive command in section" || ok "S12 no destructive commands"

echo "== v9 session watch: PASS=$PASS FAIL=$FAIL =="
[ "$FAIL" -eq 0 ]
