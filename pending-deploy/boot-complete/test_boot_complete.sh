#!/bin/bash
# test_boot_complete.sh — unit tests for the boot-complete late-boot setup.
#
# GAP IT COVERS: entrypoint.sh waited max 5 min for sys.boot_completed; a slow
# first boot skipped the whole "booted" block (no Roblox install, no helper
# scripts in the data dir, no helpers/optimizer/launch). booted-watcher.sh
# wrote the marker late, but the instance was a shell: the host watchdog's
# helper-restart failed silently on the missing files and burned the circuit
# budget into a misleading DEGRADED.
#
# Tests: waydroid-image/boot-complete.sh (new, hermetic vs fake waydroid/pgrep),
# the entrypoint.sh delegation, the Dockerfile COPY, the booted-watcher.sh
# late-boot hook, and the df-watchdog.sh missing-script guards (real watchdog
# vs fake docker/curl/systemctl, v4-test pattern).
# Exit 0 when all checks pass, 1 otherwise. Final line: "<N> passed, 0 failed".
HERE="$(cd "$(dirname "$0")" && pwd)"
IMG="$HERE/../../waydroid-image"
BC="$IMG/boot-complete.sh"
ENTRY="$IMG/entrypoint.sh"
DOCKERFILE="$IMG/Dockerfile"
BW="$IMG/booted-watcher.sh"
WD="$IMG/df-watchdog.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export T
BIN="$T/bin"; mkdir -p "$BIN"

echo "== syntax =="
bash -n "$BC" && ok "boot-complete.sh syntax" || bad "boot-complete.sh syntax"
bash -n "$ENTRY" && ok "entrypoint.sh syntax" || bad "entrypoint.sh syntax"
bash -n "$BW" && ok "booted-watcher.sh syntax" || bad "booted-watcher.sh syntax"
bash -n "$WD" && ok "df-watchdog.sh syntax" || bad "df-watchdog.sh syntax"

# ---------- fake waydroid + pgrep (Parts A & C) ----------
cat > "$BIN/waydroid" <<'EOF'
#!/bin/sh
echo "waydroid $*" >> "$T/waydroid.log"
if [ "$1 $2" = "shell pm" ] && [ "$3" = "list" ]; then
  [ "$(cat "$T/pm_installed" 2>/dev/null || echo 0)" = "1" ] && echo "package:/data/app/com.roblox.client.apk"
  exit 0
fi
if [ "$1 $2" = "shell pm" ] && [ "$3" = "install" ]; then
  if [ "$(cat "$T/pm_install_fail" 2>/dev/null || echo 0)" = "1" ]; then
    echo "Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]"
  else
    echo "Success"
  fi
  exit 0
fi
if [ "$1 $2" = "shell getprop" ]; then
  v=$(cat "$T/bootval" 2>/dev/null) || exit 1
  [ "$v" = "FAIL" ] && exit 1
  echo "$v"; exit 0
fi
exit 0
EOF
chmod +x "$BIN/waydroid"
cat > "$BIN/pgrep" <<'EOF'
#!/bin/sh
# $T/pgrep_running lists helper names that count as running
grep -qx "$2" "$T/pgrep_running" 2>/dev/null
EOF
chmod +x "$BIN/pgrep"

bc_setup() { # bc_setup: fresh data dir, fake /opt, scenario defaults
  rm -rf "$T/df" "$T/opt" "$T/apks-parent"
  mkdir -p "$T/df" "$T/opt"
  : > "$T/waydroid.log"; : > "$T/pgrep_running"
  echo 0 > "$T/pm_installed"; echo 0 > "$T/pm_install_fail"
  echo "fake" > "$T/opt/roblox-base.apk"; echo "fake" > "$T/opt/roblox-x86_64.apk"
  echo "fake" > "$T/opt/screencap-loop.sh"; echo "fake" > "$T/opt/roblox-keepalive.sh"
  echo "fake" > "$T/opt/android-optimize.sh"
}
bc_run() { # run the real boot-complete.sh, then wait for backgrounded helpers
  JERUSALEM_DATA_DIR="$T/df" BOOT_COMPLETE_OPT="$T/opt" \
    PATH="$BIN:/usr/bin:/bin" sh "$BC" > "$T/bc.out" 2>&1
  # backgrounded nohup jobs log to waydroid.log; poll briefly (no flakes)
  for _ in $(seq 1 50); do
    grep -q "shell sh /data/media/0/jerusalem/roblox-keepalive.sh" "$T/waydroid.log" 2>/dev/null && break
    sleep 0.1
  done
  sleep 0.2
}
wl() { grep -c "$1" "$T/waydroid.log" 2>/dev/null; }  # waydroid call count

echo "== A1: fresh boot, Roblox not installed -> full sequence =="
bc_setup
bc_run
[ -f "$T/df/booted" ] && ok "A1 booted marker written" || bad "A1 no booted marker"
[ "$(wl 'shell pm install /data/media/0/apks/roblox-base.apk')" = "1" ] \
  && ok "A1 pm install of both splits issued" || bad "A1 no pm install"
[ -f "$T/df/roblox_done" ] && ok "A1 roblox_done written on Success" || bad "A1 no roblox_done"
[ -f "$T/df/screencap-loop.sh" ] && [ -x "$T/df/screencap-loop.sh" ] \
  && ok "A1 canonical screencap-loop.sh installed +x" || bad "A1 screencap not installed"
[ -f "$T/df/roblox-keepalive.sh" ] && ok "A1 canonical keepalive installed" || bad "A1 keepalive not installed"
[ -f "$T/df/android-optimize.sh" ] && ok "A1 canonical optimizer installed" || bad "A1 optimizer not installed"
[ -f "$T/df/input-daemon.sh" ] && grep -q "mkfifo" "$T/df/input-daemon.sh" \
  && ok "A1 input-daemon.sh written inline" || bad "A1 input-daemon missing"
[ "$(wl 'shell sh /data/media/0/jerusalem/screencap-loop.sh')" = "1" ] \
  && ok "A1 screencap helper started" || bad "A1 screencap not started"
[ "$(wl 'shell sh /data/media/0/jerusalem/input-daemon.sh')" = "1" ] \
  && ok "A1 input daemon started" || bad "A1 input daemon not started"
[ "$(wl 'shell sh /data/media/0/jerusalem/roblox-keepalive.sh')" = "1" ] \
  && ok "A1 keepalive started" || bad "A1 keepalive not started"
[ "$(wl 'shell sh /data/media/0/jerusalem/android-optimize.sh')" = "1" ] \
  && ok "A1 optimizer run" || bad "A1 optimizer not run"
grep -q "input tap 129 424" "$T/waydroid.log" \
  && ok "A1 Roblox launch taps issued" || bad "A1 no launch taps"
grep -q "boot-complete done" "$T/bc.out" \
  && ok "A1 completes (exit path)" || bad "A1 did not complete"

echo "== A2: Roblox already installed -> no reinstall =="
bc_setup; echo 1 > "$T/pm_installed"
bc_run
[ "$(wl 'shell pm install')" = "0" ] \
  && ok "A2 no pm install when present" || bad "A2 reinstalled"
[ -f "$T/df/roblox_done" ] && ok "A2 roblox_done written" || bad "A2 no roblox_done"
[ -f "$T/df/booted" ] && ok "A2 booted written" || bad "A2 no booted"

echo "== A3: pm install fails -> WARN, no roblox_done, no crash =="
bc_setup; echo 1 > "$T/pm_install_fail"
bc_run
[ ! -f "$T/df/roblox_done" ] && ok "A3 no roblox_done on failure" || bad "A3 roblox_done on failure"
grep -q "WARN: Roblox install failed" "$T/bc.out" \
  && ok "A3 WARN logged" || bad "A3 no WARN"
[ -f "$T/df/booted" ] && ok "A3 booted still written" || bad "A3 no booted"
grep -q "boot-complete done" "$T/bc.out" \
  && ok "A3 exits 0 (never fails hard)" || bad "A3 did not complete"

echo "== A4: APKs missing from image -> WARN, skip install =="
bc_setup; rm -f "$T/opt/roblox-base.apk" "$T/opt/roblox-x86_64.apk"
bc_run
[ "$(wl 'shell pm install')" = "0" ] \
  && ok "A4 no install without APKs" || bad "A4 install attempted"
grep -q "WARN: Roblox APKs not found" "$T/bc.out" \
  && ok "A4 WARN logged" || bad "A4 no WARN"
[ -f "$T/df/booted" ] && ok "A4 booted still written" || bad "A4 no booted"

echo "== A5: canonical /opt script missing -> WARN + legacy inline fallback =="
bc_setup; rm -f "$T/opt/screencap-loop.sh" "$T/opt/roblox-keepalive.sh" "$T/opt/android-optimize.sh"
bc_run
grep -q "WARN $T/opt/screencap-loop.sh missing" "$T/bc.out" \
  && ok "A5 WARN for missing screencap" || bad "A5 no WARN"
grep -q "sleep 0.12" "$T/df/screencap-loop.sh" \
  && ok "A5 legacy inline screencap fallback written" || bad "A5 no fallback"
grep -q "mCurrentFocus" "$T/df/roblox-keepalive.sh" \
  && ok "A5 legacy inline keepalive fallback written" || bad "A5 no keepalive fallback"
grep -q "trim-caches" "$T/df/android-optimize.sh" \
  && ok "A5 legacy inline optimizer fallback written" || bad "A5 no optimizer fallback"

echo "== A6: helper already running -> not duplicated =="
bc_setup
printf 'screencap-loop.sh\ninput-daemon.sh\nroblox-keepalive.sh\n' > "$T/pgrep_running"
bc_run
[ "$(wl 'shell sh /data/media/0/jerusalem/screencap-loop.sh')" = "0" ] \
  && ok "A6 running screencap not restarted" || bad "A6 duplicate screencap"
[ "$(wl 'shell sh /data/media/0/jerusalem/input-daemon.sh')" = "0" ] \
  && ok "A6 running input daemon not restarted" || bad "A6 duplicate input"
[ "$(wl 'shell sh /data/media/0/jerusalem/roblox-keepalive.sh')" = "0" ] \
  && ok "A6 running keepalive not restarted" || bad "A6 duplicate keepalive"
grep -q "already running" "$T/bc.out" \
  && ok "A6 already-running logged" || bad "A6 no already-running log"

echo "== A7: idempotent re-run -> no reinstall, no duplicate starts =="
bc_setup; echo 1 > "$T/pm_installed"
bc_run; cp "$T/waydroid.log" "$T/first.log"
# second run: helpers now "running"
printf 'screencap-loop.sh\ninput-daemon.sh\nroblox-keepalive.sh\n' > "$T/pgrep_running"
JERUSALEM_DATA_DIR="$T/df" BOOT_COMPLETE_OPT="$T/opt" \
  PATH="$BIN:/usr/bin:/bin" sh "$BC" > "$T/bc2.out" 2>&1
sleep 0.5
[ "$(wl 'shell pm install')" = "0" ] \
  && ok "A7 no reinstall on re-run" || bad "A7 reinstalled"
[ "$(wl 'shell sh /data/media/0/jerusalem/screencap-loop.sh')" = "1" ] \
  && ok "A7 no duplicate helper starts" || bad "A7 duplicate starts"

echo "== A8: waydroid totally dead -> no crash, exit 0 =="
bc_setup
cat > "$BIN/waydroid" <<'EOF'
#!/bin/sh
echo "waydroid $*" >> "$T/waydroid.log"
exit 1
EOF
chmod +x "$BIN/waydroid"
JERUSALEM_DATA_DIR="$T/df" BOOT_COMPLETE_OPT="$T/opt" \
  PATH="$BIN:/usr/bin:/bin" sh "$BC" > "$T/bc3.out" 2>&1; rc=$?
[ "$rc" = "0" ] && ok "A8 exits 0 when waydroid is dead" || bad "A8 rc=$rc"
grep -q "boot-complete done" "$T/bc3.out" \
  && ok "A8 completes" || bad "A8 did not complete"
# restore fake waydroid for later parts
cat > "$BIN/waydroid" <<'EOF'
#!/bin/sh
echo "waydroid $*" >> "$T/waydroid.log"
if [ "$1 $2" = "shell pm" ] && [ "$3" = "list" ]; then
  [ "$(cat "$T/pm_installed" 2>/dev/null || echo 0)" = "1" ] && echo "package:/data/app/com.roblox.client.apk"
  exit 0
fi
if [ "$1 $2" = "shell pm" ] && [ "$3" = "install" ]; then
  if [ "$(cat "$T/pm_install_fail" 2>/dev/null || echo 0)" = "1" ]; then
    echo "Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]"
  else
    echo "Success"
  fi
  exit 0
fi
if [ "$1 $2" = "shell getprop" ]; then
  v=$(cat "$T/bootval" 2>/dev/null) || exit 1
  [ "$v" = "FAIL" ] && exit 1
  echo "$v"; exit 0
fi
exit 0
EOF
chmod +x "$BIN/waydroid"

echo "== B: entrypoint + Dockerfile wiring =="
grep -q "sh /opt/boot-complete.sh" "$ENTRY" \
  && ok "B1 entrypoint delegates BOOTED=1 to /opt/boot-complete.sh" || bad "B1 no delegation"
if grep -q "pm install /data/media/0/apks/roblox-base.apk" "$ENTRY"; then
  bad "B2 old inline install sequence still in entrypoint"
else
  ok "B2 old inline sequence removed from entrypoint"
fi
grep -q "^COPY boot-complete.sh /opt/boot-complete.sh" "$DOCKERFILE" \
  && ok "B3 Dockerfile COPYs boot-complete.sh" || bad "B3 no COPY line"
# every /opt/*.sh the entrypoint or watcher references must be COPY'd
missing_copy=0
for ref in $(grep -o '/opt/[a-z-]*\.sh' "$ENTRY" "$BW" | sed 's/.*://' | sort -u); do
  base=$(basename "$ref")
  grep -q "^COPY $base " "$DOCKERFILE" || { echo "  (missing COPY for $base)"; missing_copy=1; }
done
[ "$missing_copy" = "0" ] && ok "B4 all literal /opt/*.sh refs have a Dockerfile COPY" || bad "B4 COPY set incomplete"
# every $OPT/* file boot-complete.sh references must be COPY'd too
missing_opt=0
for f in roblox-base.apk roblox-x86_64.apk screencap-loop.sh roblox-keepalive.sh android-optimize.sh; do
  grep -q "^COPY $f " "$DOCKERFILE" || { echo "  (missing COPY for $f)"; missing_opt=1; }
done
[ "$missing_opt" = "0" ] && ok "B5 all boot-complete.sh \$OPT files have a Dockerfile COPY" || bad "B5 COPY set incomplete"

echo "== C: booted-watcher late-boot hook =="
bw_run() { # bw_run <iters>: bounded watcher run
  JERUSALEM_DATA_DIR="$T/df" BOOTED_WATCHER_INTERVAL=0 BOOTED_WATCHER_MAX_ITERS="$1" \
    BOOT_COMPLETE_SH="$T/fake-bc.sh" \
    PATH="$BIN:/usr/bin:/bin" sh "$BW" > "$T/bw.out" 2>&1
}
# fake boot-complete.sh: logs + touches booted (like the real one does)
cat > "$T/fake-bc.sh" <<'EOF'
#!/bin/sh
echo "fake-bc ran" >> "$T/fake-bc.log"
mkdir -p "$T/df"; touch "$T/df/booted" "$T/df/bc-setup-done"
EOF
chmod +x "$T/fake-bc.sh"

echo "-- C1: late boot + hook present -> setup runs --"
rm -rf "$T/df"; mkdir -p "$T/df"; rm -f "$T/fake-bc.log"; echo "1" > "$T/bootval"; echo 0 > "$T/pm_installed"
bw_run 2
grep -q "fake-bc ran" "$T/fake-bc.log" 2>/dev/null \
  && ok "C1 boot-complete.sh invoked on late boot" || bad "C1 hook not invoked"
[ -f "$T/df/booted" ] && ok "C1 booted marker present" || bad "C1 no booted marker"

echo "-- C2: late boot, hook script missing -> legacy touch behavior --"
rm -rf "$T/df"; mkdir -p "$T/df"; rm -f "$T/fake-bc.log"; echo "1" > "$T/bootval"
JERUSALEM_DATA_DIR="$T/df" BOOTED_WATCHER_INTERVAL=0 BOOTED_WATCHER_MAX_ITERS=2 \
  BOOT_COMPLETE_SH="$T/no-such-bc.sh" \
  PATH="$BIN:/usr/bin:/bin" sh "$BW" > "$T/bw2.out" 2>&1
[ -f "$T/df/booted" ] && ok "C2 booted marker still written (fallback)" || bad "C2 no marker"
[ ! -f "$T/fake-bc.log" ] && ok "C2 no setup without the script" || bad "C2 setup ran without script"

echo "-- C3: marker already present (fast path ran) -> hook NOT run --"
rm -rf "$T/df"; mkdir -p "$T/df"; touch "$T/df/booted"; rm -f "$T/fake-bc.log"; echo "FAIL" > "$T/bootval"
bw_run 2
[ ! -f "$T/fake-bc.log" ] && ok "C3 hook not run when marker present" || bad "C3 hook ran redundantly"
[ -f "$T/df/booted" ] && ok "C3 marker untouched" || bad "C3 marker removed!"

echo "-- C4: hook failure -> marker still written, watcher survives --"
rm -rf "$T/df"; mkdir -p "$T/df"; echo "1" > "$T/bootval"
printf '#!/bin/sh\nexit 3\n' > "$T/fake-bc.sh"; chmod +x "$T/fake-bc.sh"
JERUSALEM_DATA_DIR="$T/df" BOOTED_WATCHER_INTERVAL=0 BOOTED_WATCHER_MAX_ITERS=2 \
  BOOT_COMPLETE_SH="$T/fake-bc.sh" \
  PATH="$BIN:/usr/bin:/bin" sh "$BW" > "$T/bw4.out" 2>&1; rc=$?
[ -f "$T/df/booted" ] && ok "C4 marker written despite hook failure" || bad "C4 no marker"
[ "$rc" = "0" ] && ok "C4 watcher exits 0" || bad "C4 rc=$rc"
grep -q "WARN: boot-complete.sh" "$T/bw4.out" \
  && ok "C4 WARN logged" || bad "C4 no WARN"

echo "== D: watchdog missing-script guards (real watchdog, fake docker) =="
DBIN="$T/dbin"; DST="$T/dst"; DVOL="$T/dvol"; DLOG="$T/dwatchdog.log"; DCALLS="$T/dcalls.log"
mkdir -p "$DBIN" "$DST" "$DVOL"
cat > "$DBIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$DCALLS"
sub=$1; shift
case "$sub" in
  ps)      cat "$T/docker_ps" 2>/dev/null ;;
  inspect) date -u +%FT%TZ ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep) grep -qx "$3" "$T/pgrep_dead" 2>/dev/null && exit 1 || exit 0 ;;
      waydroid)
        case "$*" in *getprop*) echo 1; exit 0;; esac
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
EOF
chmod +x "$DBIN/docker"
cat > "$DBIN/curl" <<'EOF'
#!/bin/bash
echo 200
exit 0
EOF
chmod +x "$DBIN/curl"
cat > "$DBIN/systemctl" <<'EOF'
#!/bin/bash
case "$1" in is-active) exit 0;; start|restart) exit 0;; esac
exit 0
EOF
chmod +x "$DBIN/systemctl"
d_setup() {
  rm -rf "$DST" "$DVOL"; mkdir -p "$DST" "$DVOL"
  : > "$DCALLS"; : > "$DLOG"
  rm -f "$T/docker_ps" "$T/pgrep_dead"; : > "$T/pgrep_dead"
  echo "wd-rbx1" > "$T/docker_ps"
}
d_base() { local d="$DVOL/wd-$1-data/_data/data/media/0/jerusalem"; mkdir -p "$d"; echo "$d"; }
d_run() {
  DF_ST="$DST" DF_LOG="$DLOG" DF_INSTANCES="$1" DF_VOLBASE="$DVOL" \
    DF_LOCKDIR="$T/dlock" \
    PATH="$DBIN:/usr/bin:/bin" bash "$WD" >/dev/null 2>&1
}
export T DCALLS

echo "-- D1: helper dead + script missing -> WARN, no restart, no budget burn --"
d_setup
B=$(d_base rbx1); touch "$B/booted" "$B/live.png"
printf 'screencap-loop.sh\ninput-daemon.sh\nroblox-keepalive.sh\n' > "$T/pgrep_dead"
# NOTE: no helper .sh files in $B -> all three missing
d_run
grep -q "missing from data volume" "$DLOG" \
  && ok "D1 distinct WARN logged" || bad "D1 no WARN"
if grep -q "waydroid shell sh" "$DCALLS"; then
  bad "D1 restart attempted on missing file"
else
  ok "D1 no restart attempted"
fi
if ls "$DST"/rbx1.helper-*.ev >/dev/null 2>&1; then
  bad "D1 circuit budget burned"
else
  ok "D1 circuit budget untouched"
fi
[ -f "$DST/rbx1.degraded" ] && bad "D1 misleading DEGRADED" || ok "D1 no DEGRADED"

echo "-- D2: helper dead + script present -> restart attempted (regression) --"
d_setup
B=$(d_base rbx1); touch "$B/booted" "$B/live.png"
echo "x" > "$B/screencap-loop.sh"; echo "x" > "$B/input-daemon.sh"; echo "x" > "$B/roblox-keepalive.sh"
printf 'screencap-loop.sh\ninput-daemon.sh\nroblox-keepalive.sh\n' > "$T/pgrep_dead"
d_run
[ "$(grep -c "waydroid shell sh" "$DCALLS")" = "3" ] \
  && ok "D2 all three helpers restarted" || bad "D2 restart count wrong"
grep -q "dead -> restart" "$DLOG" \
  && ok "D2 restart logged" || bad "D2 no restart log"

echo "-- D3: stale stream + screencap script missing -> WARN, no pkill/restart --"
d_setup
B=$(d_base rbx1); touch "$B/booted"
# live.png stale (>30s): backdate it; helpers "running" so section 3 is quiet
touch -d "5 minutes ago" "$B/live.png"
: > "$T/pgrep_dead"   # nothing dead -> pgrep exits 0 (running)
echo "x" > "$B/input-daemon.sh"; echo "x" > "$B/roblox-keepalive.sh"
# screencap-loop.sh deliberately missing
d_run
grep -q "cannot restart stale stream" "$DLOG" \
  && ok "D3 distinct WARN logged" || bad "D3 no WARN"
if grep -q "pkill -f screencap-loop" "$DCALLS"; then
  bad "D3 pkill attempted on missing script"
else
  ok "D3 no pkill/restart attempted"
fi
[ -f "$DST/rbx1.staleloop.ev" ] && bad "D3 stale-loop budget burned" || ok "D3 budget untouched"

echo "-- D4: WARN dedup -> one line per day --"
d_setup
B=$(d_base rbx1); touch "$B/booted" "$B/live.png"
printf 'screencap-loop.sh\n' > "$T/pgrep_dead"
d_run; d_run
[ "$(grep -c "missing from data volume" "$DLOG")" = "1" ] \
  && ok "D4 one WARN per day" || bad "D4 WARN not deduped"

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
