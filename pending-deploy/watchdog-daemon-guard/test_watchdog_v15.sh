#!/bin/bash
# test_watchdog_v15.sh — unit tests for df-watchdog.sh v15 (docker-daemon guard).
# Runs the real watchdog against a fake `docker` (+ curl/systemctl/df).
# The gap v15 closes: a DOWN docker daemon used to look exactly like dead
# containers — `docker ps` fails fast, section 1 fired for every instance,
# and within the hour the 6/hour circuit breaker marked EVERY instance
# DEGRADED while the real fix (restart dockerd) was never attempted.
# Exit 0 when all checks pass, 1 otherwise. Final line: "PASS=n FAIL=m".
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
echo "5000 0" > "$T/uptime"  # pin high uptime: v14 boot recovery stays out (except T9)
BIN="$T/bin"; ST="$T/st"; VOL="$T/vol"; LOG="$T/watchdog.log"; CALLS="$T/calls.log"
mkdir -p "$BIN" "$ST" "$VOL"
export T CALLS   # the fakes read scenario files + call log from these

# ---- fake docker -----------------------------------------------------------
# $T/docker_info_fail: `docker info` exits 1 (daemon down).
# $T/docker_hang: one match-pattern per line; matching calls hang (killed by
#   the watchdog's `timeout`, exit 124 — a wedged daemon).
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
  info)
    [ -f "$T/docker_info_fail" ] && exit 1
    echo "Server Version: 99.0-fake"; exit 0 ;;
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
[ -f "$T/systemctl_fail" ] && exit 1
exit 0
EOF
chmod +x "$BIN/systemctl"
cat > "$BIN/df" <<'EOF'
#!/bin/bash
u=$(cat "$T/df_use" 2>/dev/null || echo 70)
printf 'Filesystem     1K-blocks      Used Available Use%% Mounted on\n/dev/sda1      100000000  92000000   8000000  %s%% /var/lib/docker\n' "$u"
EOF
chmod +x "$BIN/df"

# ---- scenario setup --------------------------------------------------------
setup() { # setup: reset all scenario files
  rm -rf "$ST" "$VOL"; mkdir -p "$ST" "$VOL"
  : > "$CALLS"; : > "$LOG"
  rm -f "$T/docker_fail" "$T/pgrep_dead" "$T/session_dead" "$T/docker_hang" \
        "$T/curl_code" "$T/df_use" "$T/docker_info_fail" "$T/systemctl_fail"
  : > "$T/docker_ps"; : > "$T/pgrep_dead"
  date -u +%FT%TZ > "$T/docker_started_at"
  echo "5000 0" > "$T/uptime"
}
run_wd() { # run_wd <instances> [max_secs]: run once; FAILS (rc!=0) on hang
  local max=${2:-60}
  DF_ST="$ST" DF_LOG="$LOG" DF_INSTANCES="$1" DF_VOLBASE="$VOL" \
    DF_LOCKDIR="$T/lock" DF_DW_FAST=1 DF_DW_ACTION=1 DF_DW_SLOW=2 \
    DF_CURL_BIN="$BIN/curl" DF_PROC_UPTIME="$T/uptime" \
    PATH="$BIN:/usr/bin:/bin" timeout "$max" bash "$SRC" >/dev/null 2>&1
}
inst_base() { # inst_base <inst>: create the instance data dir, echo it
  local d="$VOL/wd-$1-data/_data/data/media/0/jerusalem"
  mkdir -p "$d"; echo "$d"
}
healthy_inst() { # healthy_inst <name>: booted, helpers installed+alive, fresh stream
  local d; d=$(inst_base "$1")
  touch "$d/booted" "$d/live.png" \
    "$d/screencap-loop.sh" "$d/input-daemon.sh" "$d/roblox-keepalive.sh"
  echo "wd-$1" >> "$T/docker_ps"
}
down_inst() { # down_inst <name>: no container row, no markers (section-1 path)
  inst_base "$1" >/dev/null
}
syscalls() { grep -c "^systemctl $1" "$CALLS" 2>/dev/null || true; }
dockercalls() { grep -c "^docker $1" "$CALLS" 2>/dev/null || true; }

echo "== T1: healthy daemon + down instance — normal section-1 recovery intact =="
setup; down_inst rbx1
run_wd "rbx1" 60; run_wd "rbx1" 60
grep -q "^docker info" "$CALLS" && ok "daemon probed (docker info called)" || bad "daemon probe missing"
grep -q "daemon unreachable" "$LOG" && bad "spurious daemon-down log" || ok "no daemon-down log"
grep -q "docker start wd-rbx1" "$CALLS" && ok "down instance still started (2 strikes)" || bad "section-1 start lost"
grep -q "rbx1 started ok" "$LOG" && ok "start logged ok" || bad "start not logged"
[ -f "$ST/docker.degraded" ] && bad "spurious docker.degraded" || ok "no docker.degraded"
[ "$(syscalls 'restart docker')" -eq 0 ] && ok "no dockerd restart on healthy daemon" || bad "spurious dockerd restart"

echo "== T2: daemon down, cycle 1 — instance recovery skipped, no budget burn =="
setup; down_inst rbx1; touch "$T/docker_info_fail"
run_wd "rbx1" 60; rc=$?
[ "$rc" -eq 0 ] && ok "run completed (rc 0)" || bad "run rc=$rc"
grep -q "^docker info" "$CALLS" && ok "probe attempted" || bad "probe not attempted"
[ "$(dockercalls 'ps')" -eq 0 ] && ok "no docker ps while daemon down" || bad "ps ran while daemon down"
[ "$(dockercalls 'start')" -eq 0 ] && ok "no docker start while daemon down" || bad "start ran while daemon down"
[ -f "$ST/rbx1.norun" ] && bad "norun strike burned" || ok "norun strike untouched"
[ -f "$ST/rbx1.degraded" ] && bad "misleading instance DEGRADED" || ok "no instance DEGRADED"
grep -q "docker daemon unreachable (strike 1/2)" "$LOG" && ok "strike 1/2 logged" || bad "strike log missing"
[ "$(syscalls 'restart docker')" -eq 0 ] && ok "no restart before 2 strikes" || bad "restart too early"

echo "== T3: daemon down, cycle 2 — dockerd restarted, strikes reset =="
run_wd "rbx1" 60
[ "$(syscalls 'restart docker')" -eq 1 ] && ok "systemctl restart docker called once" || bad "restart count: $(syscalls 'restart docker')"
grep -q "docker restarted ok" "$LOG" && ok "restart logged ok" || bad "restart not logged"
[ -f "$ST/docker.daemondown" ] && bad "strikes not reset after attempt" || ok "strikes reset after attempt"
[ -f "$ST/docker.degraded" ] && bad "premature docker.degraded" || ok "no docker.degraded yet"

echo "== T4: daemon down, budget trip — 2 restarts/hour max, then alert-only =="
setup; down_inst rbx1; touch "$T/docker_info_fail"
for _ in 1 2 3 4 5 6; do run_wd "rbx1" 60; done
[ "$(syscalls 'restart docker')" -eq 2 ] && ok "exactly 2 dockerd restarts in the hour" || bad "restart count: $(syscalls 'restart docker')"
[ -f "$ST/docker.degraded" ] && ok "docker.degraded marker written on trip" || bad "no docker.degraded on trip"
grep -q "docker DEGRADED" "$LOG" && ok "DEGRADED logged" || bad "DEGRADED not logged"
[ -f "$ST/rbx1.degraded" ] && bad "instance DEGRADED despite daemon gate" || ok "no instance DEGRADED after trip"
run_wd "rbx1" 60
[ "$(syscalls 'restart docker')" -eq 2 ] && ok "no restart after trip (alert-only)" || bad "restart after trip"
[ "$(grep -c 'docker DEGRADED' "$LOG")" -eq 1 ] && ok "DEGRADED logged once, no spam" || bad "DEGRADED spam: $(grep -c 'docker DEGRADED' "$LOG")"

echo "== T5: daemon recovers — marker cleared, recovery resumes =="
rm -f "$T/docker_info_fail"
healthy_inst rbx1
run_wd "rbx1" 60
[ -f "$ST/docker.degraded" ] && bad "marker not cleared on recovery" || ok "marker cleared on recovery"
grep -q "docker daemon recovered from DEGRADED" "$LOG" && ok "recovery logged" || bad "recovery not logged"
grep -q "^docker ps" "$CALLS" && ok "instance checks resumed (ps called)" || bad "instance checks not resumed"

echo "== T6: daemon probe HANGS (wedged daemon) — bounded, treated as down =="
setup; down_inst rbx1; echo "info" > "$T/docker_hang"
start=$(date +%s)
run_wd "rbx1" 60; rc=$?
elapsed=$(( $(date +%s) - start ))
[ "$rc" -eq 0 ] && ok "run completed despite hung probe (rc 0)" || bad "run hung (rc=$rc)"
[ "$elapsed" -lt 45 ] && ok "run bounded (${elapsed}s < 45s)" || bad "run took ${elapsed}s"
grep -q "docker daemon unreachable" "$LOG" && ok "hung probe counts as daemon down" || bad "hung probe not treated as down"
[ "$(dockercalls 'start')" -eq 0 ] && ok "no start against a wedged daemon" || bad "start ran on wedged daemon"

echo "== T7: dockerd restart FAILS — logged, strikes reset, no marker yet =="
setup; down_inst rbx1; touch "$T/docker_info_fail" "$T/systemctl_fail"
run_wd "rbx1" 60; run_wd "rbx1" 60
grep -q "docker restart FAILED" "$LOG" && ok "failed restart logged" || bad "failed restart not logged"
[ -f "$ST/docker.daemondown" ] && bad "strikes not reset after failed attempt" || ok "strikes reset after failed attempt"
[ -f "$ST/docker.degraded" ] && bad "premature docker.degraded on failed restart" || ok "no docker.degraded yet"

echo "== T8: daemon down + disk 95% — prune skipped, budget untouched, alert fires =="
setup; down_inst rbx1; touch "$T/docker_info_fail"; echo 95 > "$T/df_use"
run_wd "rbx1" 60
[ "$(dockercalls 'image prune')" -eq 0 ] && ok "no image prune while daemon down" || bad "prune ran while daemon down"
[ -f "$ST/disk.prune.ev" ] && bad "disk budget burned while daemon down" || ok "disk budget untouched"
grep -q "auto-prune skipped (docker daemon unreachable)" "$LOG" && ok "prune skip logged" || bad "prune skip not logged"
grep -q "WARN disk 95% on /var/lib/docker (alert only)" "$LOG" && ok "disk pressure alert still fires" || bad "disk alert hidden"
echo "== T8b: daemon healthy + disk 95% — prune still runs (regression) =="
setup; down_inst rbx1; echo 95 > "$T/df_use"
run_wd "rbx1" 60
[ "$(dockercalls 'image prune')" -ge 1 ] && ok "prune runs on healthy daemon" || bad "prune lost on healthy daemon"
[ -f "$ST/disk.prune.ev" ] && ok "disk budget counted on healthy daemon" || bad "disk budget not counted"

echo "== T9: daemon down during fresh boot — boot recovery skipped too =="
setup; down_inst rbx1; touch "$T/docker_info_fail"; echo "100 0" > "$T/uptime"
run_wd "rbx1" 60
grep -q "boot recovery" "$LOG" && bad "boot recovery ran while daemon down" || ok "boot recovery skipped"
[ "$(dockercalls 'start')" -eq 0 ] && ok "no boot start while daemon down" || bad "boot start ran while daemon down"

echo "== T10: source contracts =="
grep -q 'dw "\$DW_FAST" info' "$SRC" && ok "daemon probe is dw-bounded (info)" || bad "probe not dw-bounded"
grep -q 'cb_guard "docker" "docker.restart" 3600 2 "docker daemon"' "$SRC" && ok "2/hour dockerd restart budget" || bad "restart budget missing"
grep -q 'if \[ "\$DOCKER_DOWN" = 0 \]; then' "$SRC" && ok "docker-dependent sections gated" || bad "gate missing"
grep -q 'auto-prune skipped (docker daemon unreachable)' "$SRC" && ok "disk prune gate present" || bad "disk gate missing"
n=$(grep -c '^ *docker exec "\$c" nohup' "$SRC")
[ "$n" -eq 3 ] && ok "3 intentionally-unbounded helper restarts (bare docker; + missing-frame-guard loop restart)" || bad "bare restarts: $n (want 3)"
bash -n "$SRC" && ok "bash -n clean" || bad "bash -n failed"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
