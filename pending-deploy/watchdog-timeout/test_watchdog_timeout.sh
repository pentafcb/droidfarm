#!/bin/bash
# test_watchdog_timeout.sh — unit tests for df-watchdog.sh v13 (bounded docker calls).
# Runs the real watchdog against a fake `docker` (+ curl/systemctl/df) with
# scenario files. The fake can HANG selected subcommands (exec sleep, killed
# by the watchdog's `timeout`), proving a wedged docker can no longer freeze
# the minute-cron run forever.
# Exit 0 when all checks pass, 1 otherwise. Final line: "PASS=n FAIL=m".
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
echo "5000 0" > "$T/uptime"  # pin high uptime: v14 boot recovery stays out of these dw-timeout tests
BIN="$T/bin"; ST="$T/st"; VOL="$T/vol"; LOG="$T/watchdog.log"; CALLS="$T/calls.log"
mkdir -p "$BIN" "$ST" "$VOL"
export T CALLS   # the fakes read scenario files + call log from these

# ---- fake docker -----------------------------------------------------------
# $T/docker_hang: one match-pattern per line; when "$sub $*" contains a
# pattern the fake hangs (exec sleep -> timeout's TERM kills it directly).
# Other scenario files: docker_ps, docker_started_at, docker_fail,
# pgrep_dead (names, one per line), session_dead, df_use (Use%).
cat > "$BIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
sub=$1; shift
if [ -f "$T/docker_hang" ]; then
  while IFS= read -r pat || [ -n "$pat" ]; do
    [ -z "$pat" ] && continue
    case "$sub $*" in *"$pat"*) exec sleep 60;; esac
  done < "$T/docker_hang"
fi
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
      waydroid) # `waydroid shell getprop ...` / `waydroid shell dumpsys ...`
        case "$*" in
          *getprop*) [ -f "$T/session_dead" ] && exit 1; echo 1; exit 0;;
          *dumpsys*) echo "versionName=2.740.931"; exit 0;;
        esac
        exit 0 ;;
      *) exit 0 ;;  # pkill, session start (-d), nohup restarts
    esac ;;
  image|builder) echo "Total reclaimed space: 0B"; exit 0 ;;
esac
exit 0
EOF
chmod +x "$BIN/docker"

# ---- fake curl / systemctl / df (keep the suite hermetic) -------------------
cat > "$BIN/curl" <<'EOF'
#!/bin/bash
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
cat > "$BIN/df" <<'EOF'
#!/bin/bash
u=$(cat "$T/df_use" 2>/dev/null || echo 70)
printf 'Filesystem     1K-blocks      Used Available Use%% Mounted on\n/dev/sda1      100000000  92000000   8000000  %s%% /var/lib/docker\n' "$u"
EOF
chmod +x "$BIN/df"

# ---- no-timeout PATH: symlinks to the essentials, but NO timeout -----------
NOBIN="$T/nobin"; mkdir -p "$NOBIN"
for t in date stat mkdir rm cat grep tail chmod mv awk; do
  p=$(command -v "$t" 2>/dev/null) && ln -s "$p" "$NOBIN/$t"
done

# ---- scenario setup --------------------------------------------------------
setup() { # setup: reset all scenario files
  rm -rf "$ST" "$VOL"; mkdir -p "$ST" "$VOL"
  : > "$CALLS"; : > "$LOG"
  rm -f "$T/docker_fail" "$T/pgrep_dead" "$T/session_dead" "$T/docker_hang" \
        "$T/curl_code" "$T/df_use"
  : > "$T/docker_ps"; : > "$T/pgrep_dead"
  date -u +%FT%TZ > "$T/docker_started_at"
}
inst_base() { # inst_base <inst>: create the instance data dir, echo it
  local d="$VOL/wd-$1-data/_data/data/media/0/jerusalem"
  mkdir -p "$d"; echo "$d"
}
run_wd() { # run_wd <instances> [max_secs]: run once; FAILS (rc!=0) on hang
  local max=${2:-90}
  DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="$1" DF_VOLBASE="$VOL" \
    DF_LOCKDIR="$T/lock" DF_DW_FAST=1 DF_DW_ACTION=1 DF_DW_SLOW=2 \
    DF_CURL_BIN="$BIN/curl" DF_PROC_UPTIME="$T/uptime" \
    PATH="$BIN:/usr/bin:/bin" timeout "$max" bash "$SRC" >/dev/null 2>&1
}
run_wd_notimeout() { # same, but the WATCHDOG's PATH has no `timeout` binary
  # (fallback branch); the outer guard uses the absolute timeout path.
  DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="$1" DF_VOLBASE="$VOL" \
    DF_LOCKDIR="$T/lock" DF_DW_FAST=1 DF_DW_ACTION=1 DF_DW_SLOW=2 \
    DF_CURL_BIN="$BIN/curl" \
    PATH="$NOBIN:$BIN" /usr/bin/timeout 90 /bin/bash "$SRC" >/dev/null 2>&1
}
healthy_inst() { # healthy_inst <name>: booted, helpers installed+alive, fresh stream
  local d; d=$(inst_base "$1")
  touch "$d/booted" "$d/live.png" \
    "$d/screencap-loop.sh" "$d/input-daemon.sh" "$d/roblox-keepalive.sh"
  echo "wd-$1" >> "$T/docker_ps"
}

echo "== T1: healthy baseline — no WARNs, no recoveries, run completes =="
setup; healthy_inst rbx1
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run completed (rc 0)" || bad "run rc=$rc (hung?)"
grep -q "timed out" "$LOG" && bad "unexpected timeout WARN" || ok "no timeout WARN"
grep -q "session start" "$CALLS" && bad "unexpected session start" || ok "no session start issued"
grep -q "dead -> restart" "$LOG" && bad "unexpected helper restart" || ok "no helper restarts"
[ -f "$ST/rbx1.degraded" ] && bad "unexpected .degraded" || ok "no .degraded marker"

echo "== T2: session probe hangs -> run bounded, unknown-skip, no false recovery =="
setup; healthy_inst rbx1
echo "getprop" > "$T/docker_hang"
start=$(date +%s)
run_wd "rbx1" 60; rc=$?
elapsed=$(( $(date +%s) - start ))
[ "$rc" -eq 0 ] && ok "run completed despite hung probe (rc 0)" || bad "run hung (rc=$rc)"
[ "$elapsed" -lt 45 ] && ok "run bounded (${elapsed}s < 45s)" || bad "run took ${elapsed}s"
grep -q "docker exec wd-rbx1 waydroid shell getprop" "$CALLS" && ok "probe was attempted" || bad "probe not attempted"
grep -q "session start" "$CALLS" && bad "session start issued on unknown state" || ok "no session start on unknown"
grep -q "WARN: docker session probe timed out" "$LOG" && ok "one WARN names the session probe" || bad "missing session-probe WARN"
[ -f "$ST/rbx1.session.ev" ] && bad "session circuit budget burned" || ok "session budget untouched"
[ -f "$ST/rbx1.degraded" ] && bad "DEGRADED on unknown state" || ok "no DEGRADED on unknown"
run_wd "rbx1" 60
[ "$(grep -c "timed out" "$LOG")" -eq 1 ] && ok "WARN deduped (one per day)" || bad "WARN not deduped: $(grep -c 'timed out' "$LOG")"

echo "== T3: session probe FAILS FAST -> still dead-session recovery (regression) =="
setup; healthy_inst rbx1
touch "$T/session_dead"   # fast failure, not a hang
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run completed" || bad "run rc=$rc"
grep -q "session start issued" "$LOG" && ok "fast failure still triggers session start" || bad "dead-session path lost"
[ -f "$ST/rbx1.sessionstart.ts" ] && ok "grace ts written" || bad "grace ts missing"
grep -q "timed out" "$LOG" && bad "fast failure misread as timeout" || ok "fast failure not a timeout"

echo "== T4: helper pgrep hangs -> no restart, WARN, run bounded =="
setup; healthy_inst rbx1
echo "pgrep" > "$T/docker_hang"   # getprop fast (alive), pgrep hangs
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run completed" || bad "run rc=$rc"
grep -q "dead -> restart" "$LOG" && bad "restart issued for unknown helper" || ok "no restart on unknown"
grep -q "docker exec wd-rbx1 nohup" "$CALLS" && bad "restart exec reached docker" || ok "no restart exec"
grep -q "WARN: docker helper probe (screencap-loop.sh) timed out" "$LOG" && ok "helper WARN names the helper" || bad "missing helper WARN"
[ -f "$ST"/rbx1.helper-*.ev ] && bad "helper circuit budget burned" || ok "helper budget untouched"

echo "== T5: helper genuinely dead (fast) -> restart still issued (regression) =="
setup; healthy_inst rbx1
echo "screencap-loop.sh" > "$T/pgrep_dead"
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run completed" || bad "run rc=$rc"
grep -q "screencap-loop.sh dead -> restart" "$LOG" && ok "dead helper restarted" || bad "dead-helper path lost"
grep -q "timed out" "$LOG" && bad "fast pgrep misread as timeout" || ok "no spurious timeout"

echo "== T6: docker ps hangs -> bounded, normal down/strike path =="
setup; healthy_inst rbx1
echo "ps --format" > "$T/docker_hang"
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run 1 completed (strike 1, no start yet)" || bad "run 1 rc=$rc"
grep -q "started ok" "$LOG" && bad "start on first strike" || ok "no start before 2 strikes"
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run 2 completed" || bad "run 2 rc=$rc"
grep -q "started ok" "$LOG" && ok "start attempted after 2 strikes, bounded" || bad "no start after 2 strikes"

echo "== T7: no timeout binary -> bare-docker fallback, healthy run =="
setup; healthy_inst rbx1
run_wd_notimeout "rbx1"; rc=$?
[ "$rc" -eq 0 ] && ok "run completed without timeout binary" || bad "run rc=$rc"
grep -q "timed out" "$LOG" && bad "spurious timeout WARN" || ok "no timeout WARN"
[ -f "$ST/rbx1.degraded" ] && bad "unexpected .degraded" || ok "no .degraded marker"
grep -q "docker exec wd-rbx1 waydroid shell getprop" "$CALLS" && ok "probes still ran via fallback" || bad "probes did not run"

echo "== T8: prune hangs at 95% disk -> bounded, run completes =="
setup; healthy_inst rbx1
echo 95 > "$T/df_use"
echo "prune" > "$T/docker_hang"
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run completed despite hung prune" || bad "run rc=$rc"
grep -q "docker image prune" "$CALLS" && ok "prune was attempted" || bad "prune not attempted"
grep -q "safe prune" "$LOG" && ok "prune outcome logged" || bad "prune not logged"

echo "== T9: dumpsys hangs -> version check silently skipped =="
setup; healthy_inst rbx1
echo "dumpsys" > "$T/docker_hang"
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run completed" || bad "run rc=$rc"
[ -f "$ST/rbx1.roblox_drift" ] && bad "drift marker on unknown version" || ok "no drift marker"
grep -q "DRIFT" "$LOG" && bad "drift ALERT on unknown version" || ok "no drift ALERT"

echo "== T10: session start hangs -> FAILED logged, no grace ts =="
setup; healthy_inst rbx1
touch "$T/session_dead"
echo "session start" > "$T/docker_hang"
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run completed" || bad "run rc=$rc"
grep -q "session start FAILED" "$LOG" && ok "hung start reported FAILED" || bad "missing FAILED log"
[ -f "$ST/rbx1.sessionstart.ts" ] && bad "grace ts written for failed start" || ok "no grace ts on failure"

echo "== T11: unknown probes block DEGRADED auto-clear; recovery clears it =="
setup; healthy_inst rbx1
echo "test-degraded" > "$ST/rbx1.degraded"
echo "getprop" > "$T/docker_hang"
run_wd "rbx1" 60
[ -f "$ST/rbx1.degraded" ] && ok ".degraded kept while probes blind" || bad ".degraded auto-cleared while blind"
rm "$T/docker_hang"   # probes healthy again
run_wd "rbx1" 60
[ -f "$ST/rbx1.degraded" ] && bad ".degraded not cleared after recovery" || ok ".degraded cleared when all green"
grep -q "recovered from DEGRADED" "$LOG" && ok "recovery logged" || bad "recovery not logged"

echo "== T12: source contracts =="
n=$(grep -c 'dw "\$DW_' "$SRC")
[ "$n" -eq 16 ] && ok "16 dw-bounded docker sites (13 + v15 daemon probe + missing-frame-guard pgrep/pkill)" || bad "dw sites: $n (want 16)"
n=$(grep -c '^ *docker exec "\$c" nohup' "$SRC")
[ "$n" -eq 3 ] && ok "3 intentionally-unbounded helper restarts (bare docker; + missing-frame-guard loop restart)" || bad "bare restarts: $n (want 3)"
grep -q '_probe_timeout_warn() {' "$SRC" && ok "warn helper defined" || bad "warn helper missing"
grep -q 'DF_DW_FAST\|DF_DW_ACTION\|DF_DW_SLOW' "$SRC" && ok "timeout env overrides documented" || bad "overrides missing"
bash -n "$SRC" && ok "bash -n clean" || bad "bash -n failed"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
