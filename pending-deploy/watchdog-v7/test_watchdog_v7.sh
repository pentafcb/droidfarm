#!/bin/bash
# test_watchdog_v7.sh — unit tests for df-watchdog.sh v7 memory-pressure watch:
#   (a) MemAvailable >=1GB -> silent, clears a stale mem.degraded marker;
#   (b) MemAvailable <1GB 3x in an hour -> WARN x2, then mem.degraded marker
#       carrying the reason on its first line; no auto-kill anywhere;
#   (c) relief -> marker + event file cleared, "resumed" logged;
#   (d) fresh "Out of memory" lines in dmesg -> one loud WARN, cursor updated.
# Runs ONLY section 9, extracted verbatim from the real script, against stub
# log/note_event and temp state dirs. Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
ST="$T/st"; LOG="$T/watchdog.log"; MEMINFO="$T/meminfo"; DMESG="$T/dmesg.out"
mkdir -p "$ST"

START=$(grep -n "# 9. memory pressure" "$SRC" | cut -d: -f1)
END=$(grep -n "^exit 0" "$SRC" | cut -d: -f1)
if [ -z "$START" ] || [ -z "$END" ] || [ "$START" -ge "$END" ]; then
  bad "could not locate section 9 in $SRC"; echo "FAIL=$FAIL PASS=$PASS"; exit 1
fi

run_section() {
  # stubs + verbatim section, in a subshell with its own ST
  {
    echo 'log() { echo "$1" >> "$LOG"; }'
    echo 'note_event() { local key=$1 w=$2 n=0 t; local f="$ST/$key.ev";'
    echo '  now=$(date +%s); [ -f "$f" ] && { while IFS= read -r t; do case "$t" in ""|*[!0-9]*) continue;; esac; [ $((now-t)) -lt "$w" ] && n=$((n+1)); done < "$f"; };'
    echo '  echo "$now" >> "$f"; echo $((n+1)); }'
    echo "ST=\"$ST\" LOG=\"$LOG\""
    sed -n "${START},$((END-1))p" "$SRC"
  } > "$T/section.sh"
  bash "$T/section.sh"
}

set_mem_kb() { printf 'MemTotal:       32590000 kB\nMemAvailable:  %8s kB\n' "$1" > "$MEMINFO"; }
set_dmesg()  { printf '%s' "$1" > "$DMESG"; }
export DF_PROC_MEMINFO="$MEMINFO" DF_DMESG="$DMESG"

# (a) healthy host: silent, no marker
set_mem_kb 20000000; set_dmesg "quiet"
run_section
[ -s "$LOG" ] && bad "healthy host should be silent" || ok "healthy host silent"
[ ! -f "$ST/mem.degraded" ] && ok "no marker on healthy host" || bad "marker on healthy host"

# (b) low memory 3x -> WARN, WARN, DEGRADED
set_mem_kb 500000
run_section; run_section
grep -q "WARN memory pressure" "$LOG" && ok "WARN logged on low memory" || bad "no WARN on low memory"
[ ! -f "$ST/mem.degraded" ] && ok "no marker before 3rd strike" || bad "marker too early"
run_section
[ -f "$ST/mem.degraded" ] && ok "mem.degraded marker on 3rd strike" || bad "no marker on 3rd strike"
grep -q "needs human" "$ST/mem.degraded" && ok "marker carries reason" || bad "marker lacks reason"
grep -q "docker stop\|docker kill\|docker rm" "$LOG" "$T/section.sh" 2>/dev/null \
  && bad "section must never kill containers" || ok "alert-only: no kill commands"

# (c) relief clears marker
set_mem_kb 20000000
run_section
[ ! -f "$ST/mem.degraded" ] && ok "marker cleared on relief" || bad "marker not cleared"
grep -q "memory pressure relieved" "$LOG" && ok "relief logged" || bad "relief not logged"

# (d) OOM lines surface a WARN
rm -f "$ST/oom.count"
set_dmesg "[12345.6] Out of memory: Killed process 999 (waydroid) total-vm:1kB
[12346.7] Out of memory: Killed process 1000 (waydroid) total-vm:1kB"
run_section
grep -q "OOM killer fired 2x" "$LOG" && ok "OOM firing surfaced" || bad "OOM not surfaced"
run_section
[ "$(grep -c 'OOM killer fired' "$LOG")" = "1" ] && ok "OOM WARN deduped by cursor" || bad "OOM WARN repeated"

echo "== v7 memory watch: PASS=$PASS FAIL=$FAIL =="
[ "$FAIL" -eq 0 ]
