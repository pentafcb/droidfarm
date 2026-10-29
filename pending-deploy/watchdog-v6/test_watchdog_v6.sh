#!/bin/bash
# test_watchdog_v6.sh — unit tests for df-watchdog.sh v6:
#   (a) disk auto-recovery: >=90% -> bounded SAFE prune (dangling images +
#       build cache only; never volumes, never image prune -a), budgeted at
#       >2 prunes/hour -> disk.degraded marker + alert-only, auto-clear <90%;
#   (b) Roblox version-pin guard: drift vs PINNED_ROBLOX -> one ALERT per
#       drifted version (deduped), "back on pinned" on return, silent when
#       the package is absent or dumpsys fails, skipped when container down.
# Runs the real watchdog against fake `docker`/`df`/`curl`/`systemctl` +
# temp state/log/volume dirs. No real services touched.
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

# ---- fake docker ----------------------------------------------------------
cat > "$BIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
sub=$1; shift
case "$sub" in
  ps)      cat "$T/docker_ps" 2>/dev/null ;;
  inspect) cat "$T/docker_started_at" 2>/dev/null ;;
  start|restart) exit 0 ;;
  image)
    [ "$1" = "prune" ] || exit 0
    [ -f "$T/prune_fail" ] && exit 1
    echo "Total reclaimed space: 1.234GB"
    exit 0 ;;
  builder)
    [ "$1" = "prune" ] || exit 0
    [ -f "$T/prune_fail" ] && exit 1
    exit 0 ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep) grep -qx "$3" "$T/pgrep_dead" 2>/dev/null && exit 1 || exit 0 ;;
      waydroid)
        # v9 session probe: `waydroid shell getprop sys.boot_completed`
        case "$*" in *getprop*) [ -f "$T/session_dead" ] && exit 1; echo 1; exit 0;; esac
        # waydroid shell dumpsys package com.roblox.client
        if [ -f "$T/roblox_fail" ]; then exit 1; fi
        if [ -f "$T/roblox_absent" ]; then
          echo "Unable to find package com.roblox.client"; exit 0
        fi
        v=2.740.931; [ -f "$T/roblox_version" ] && v=$(cat "$T/roblox_version")
        printf 'Package [com.roblox.client]:\n  versionCode=1234567\n  versionName=%s\n' "$v"
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
EOF
chmod +x "$BIN/docker"

# ---- fake df: $T/df_use = Use%; $T/df_relief -> 92 on first read, 70 after --
cat > "$BIN/df" <<'EOF'
#!/bin/bash
n=$(cat "$T/df_calls" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$T/df_calls"
u=$(cat "$T/df_use" 2>/dev/null || echo 70)
if [ -f "$T/df_relief" ] && [ "$n" -gt 1 ]; then u=70; fi
printf 'Filesystem     1K-blocks      Used Available Use%% Mounted on\n/dev/sda1      100000000  92000000   8000000  %s%% /var/lib/docker\n' "$u"
EOF
chmod +x "$BIN/df"

# ---- fake curl: healthy manager (200) --------------------------------------
cat > "$BIN/curl" <<'EOF'
#!/bin/bash
echo 200; exit 0
EOF
chmod +x "$BIN/curl"

# ---- fake systemctl --------------------------------------------------------
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
setup() {
  rm -rf "$ST" "$VOL"; mkdir -p "$ST" "$VOL"
  : > "$CALLS"; : > "$LOG"; rm -f "$T/df_calls"
  rm -f "$T/prune_fail" "$T/roblox_fail" "$T/roblox_absent" "$T/roblox_version" "$T/df_relief"
  echo "wd-rbx1" > "$T/docker_ps"          # healthy container
  : > "$T/pgrep_dead"                       # all helpers alive
  date -u +%FT%TZ > "$T/docker_started_at"
  echo 70 > "$T/df_use"                     # healthy disk
  local d="$VOL/wd-rbx1-data/_data/data/media/0/jerusalem"
  mkdir -p "$d"; touch "$d/booted" "$d/live.png"
}
run_wd() { # run_wd: run the watchdog once
  DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="rbx1" DF_VOLBASE="$VOL" \
    DF_PINNED_ROBLOX="2.740.931" DF_LOCKDIR="$T/lock" DF_PROC_UPTIME="$T/uptime" \
    PATH="$BIN:/usr/bin:/bin" bash "$SRC" >/dev/null 2>&1
}
seed_events() { # seed_events <key> <count> <age_secs>
  local f="$ST/$1.ev" n=$2 age=$3 now i
  now=$(date +%s); : > "$f"
  for i in $(seq 1 "$n"); do echo $((now - age)) >> "$f"; done
}

echo "== bash -n =="
bash -n "$SRC" && ok "syntax" || bad "syntax"

echo "== D1 healthy disk (70%): no prune, no alert =="
setup
run_wd
grep -q "prune" "$CALLS" && bad "D1 prune called" || ok "D1 no prune"
grep -q -i "disk" "$LOG" && bad "D1 disk mentioned" || ok "D1 log silent"

echo "== D2 disk 92% -> safe prune, pressure relieved =="
setup
echo 92 > "$T/df_use"; touch "$T/df_relief"   # 92 first read, 70 after
run_wd
grep -q "docker image prune -f" "$CALLS" && ok "D2 image prune (dangling only)" || bad "D2 image prune missing"
grep -q "docker builder prune -f" "$CALLS" && ok "D2 builder prune" || bad "D2 builder prune missing"
grep -q "docker volume prune" "$CALLS" && bad "D2 volume prune called!" || ok "D2 no volume prune"
grep -q "image prune -a" "$CALLS" && bad "D2 image prune -a called!" || ok "D2 no image prune -a"
grep -q "disk 92% -> safe prune" "$LOG" && ok "D2 prune logged with before%" || bad "D2 prune not logged"
grep -q "now 70%" "$LOG" && ok "D2 after% logged" || bad "D2 after% missing"
grep -q "1.234GB" "$LOG" && ok "D2 freed space logged" || bad "D2 freed space missing"
grep -q "WARN disk" "$LOG" && bad "D2 WARN after relief" || ok "D2 no WARN (relieved)"
grep -q "1/2 per hour" "$LOG" && ok "D2 budget counter logged" || bad "D2 budget counter missing"

echo "== D3 disk 92% stays 92%: prune runs, still-high alert kept =="
setup
echo 92 > "$T/df_use"   # no relief -> every read 92
run_wd
grep -q "docker image prune -f" "$CALLS" && ok "D3 prune attempted" || bad "D3 prune missing"
grep -q "WARN disk 92%" "$LOG" && ok "D3 still-high WARN kept" || bad "D3 WARN missing"

echo "== D4 prune storm -> disk.degraded, no more pruning =="
setup
echo 92 > "$T/df_use"
seed_events "disk.prune" 2 60
run_wd
grep -q "prune -f" "$CALLS" && bad "D4 prune past budget" || ok "D4 no prune past budget"
[ -f "$ST/disk.degraded" ] && ok "D4 degraded marker set" || bad "D4 marker missing"
head -1 "$ST/disk.degraded" | grep -q "auto-prune paused" && ok "D4 reason on first line" || bad "D4 reason missing"
grep -q "disk DEGRADED" "$LOG" && ok "D4 DEGRADED logged" || bad "D4 DEGRADED not logged"
DEG_LINES=$(grep -c "disk DEGRADED" "$LOG")
run_wd
[ "$(grep -c "disk DEGRADED" "$LOG")" -eq "$DEG_LINES" ] && ok "D4 no DEGRADED spam" || bad "D4 DEGRADED logged again"
grep -q "auto-prune paused (budget exhausted" "$LOG" && ok "D4 paused line logged" || bad "D4 paused line missing"
grep -q "prune -f" "$CALLS" && bad "D4 prune while degraded" || ok "D4 still no prune while degraded"

echo "== D5 pressure relief clears marker =="
setup
echo 70 > "$T/df_use"
echo "disk auto-prune ran 3 times - auto-recovery paused" > "$ST/disk.degraded"
seed_events "disk.prune" 2 60
run_wd
[ -f "$ST/disk.degraded" ] && bad "D5 marker not cleared" || ok "D5 marker cleared"
ls "$ST"/disk.prune.ev >/dev/null 2>&1 && bad "D5 event file not cleaned" || ok "D5 event file cleaned"
grep -q "disk pressure relieved" "$LOG" && ok "D5 relief logged" || bad "D5 relief not logged"

echo "== D6 prune failure: logged, run exits clean =="
setup
echo 92 > "$T/df_use"; touch "$T/prune_fail"
run_wd
echo "rc=$?"
grep -q "safe prune" "$LOG" && ok "D6 attempt logged" || bad "D6 attempt not logged"

echo "== R1 version matches pinned: silent =="
setup
run_wd
grep -q -i "roblox.*drift\|ALERT" "$LOG" && bad "R1 drift mentioned" || ok "R1 silent"
[ -f "$ST/rbx1.robloxver" ] && bad "R1 state file written" || ok "R1 no state file"

echo "== R2 version drift: one loud ALERT =="
setup
echo "2.741.100" > "$T/roblox_version"
run_wd
grep -q "ALERT rbx1 Roblox client version DRIFT: installed 2.741.100 (pinned 2.740.931)" "$LOG" \
  && ok "R2 ALERT with versions" || bad "R2 ALERT missing/wrong"
[ "$(cat "$ST/rbx1.robloxver" 2>/dev/null)" = "2.741.100" ] && ok "R2 drift version recorded" || bad "R2 state wrong"

echo "== R3 same drift again: no repeat ALERT =="
run_wd
[ "$(grep -c "ALERT rbx1 Roblox client version DRIFT" "$LOG")" -eq 1 ] && ok "R3 deduped" || bad "R3 ALERT repeated"

echo "== R4 drift to a NEW version: new ALERT =="
echo "2.742.1" > "$T/roblox_version"
run_wd
[ "$(grep -c "ALERT rbx1 Roblox client version DRIFT" "$LOG")" -eq 2 ] && ok "R4 new-version ALERT" || bad "R4 no new ALERT"
grep -q "installed 2.742.1 (pinned 2.740.931)" "$LOG" && ok "R4 names new version" || bad "R4 wrong version"

echo "== R5 back to pinned: recovery logged, state cleared =="
rm "$T/roblox_version"
run_wd
grep -q "rbx1 Roblox client back on pinned build 2.740.931" "$LOG" && ok "R5 recovery logged" || bad "R5 recovery not logged"
[ -f "$ST/rbx1.robloxver" ] && bad "R5 state not cleared" || ok "R5 state cleared"

echo "== R6 package absent: silent skip =="
setup
touch "$T/roblox_absent"
run_wd
grep -q -i "roblox" "$LOG" && bad "R6 roblox mentioned" || ok "R6 silent"

echo "== R7 dumpsys fails: silent skip, run clean =="
setup
touch "$T/roblox_fail"
run_wd
grep -q -i "roblox" "$LOG" && bad "R7 roblox mentioned" || ok "R7 silent"

echo "== R8 container down: no version check attempted =="
setup
: > "$T/docker_ps"   # container down
run_wd
grep -q "dumpsys" "$CALLS" && bad "R8 dumpsys called" || ok "R8 no dumpsys when down"
grep -q -i "roblox.*drift\|ALERT" "$LOG" && bad "R8 drift mentioned" || ok "R8 silent"

echo "== regression: v4 + v5 suites still green =="
bash "$HERE/../watchdog-v4/test_watchdog_v4.sh" > /tmp/wd6-reg-v4.log 2>&1
grep -q "PASS=26 FAIL=0" /tmp/wd6-reg-v4.log && ok "v4 suite 26/26" || bad "v4 suite regressed"
bash "$HERE/../watchdog-v5/test_watchdog_v5.sh" > /tmp/wd6-reg-v5.log 2>&1
grep -q "PASS=27 FAIL=0" /tmp/wd6-reg-v5.log && ok "v5 suite 27/27" || bad "v5 suite regressed"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
