#!/bin/bash
# test_roblox_install_guard.sh — unit tests for the Roblox install-failure
# guard (boot-complete.sh marker + df-watchdog.sh check 3c mirroring).
#
# GAP IT COVERS: boot-complete.sh's one-shot `pm install` could fail (bad
# APKs, package-manager failure), time out (wedged session), or find no
# APKs in the image — and wrote NO marker, only one WARN in the container
# log. The dashboard card then sat on "installing_roblox" forever with zero
# alert (the keepalive skips uninstalled packages, so nothing ever retried
# or reported it). Now boot-complete.sh writes roblox_install_failed
# (reason on first line) on every failure path and clears it when the
# client is present; the watchdog mirrors it into the instance .degraded
# marker (dashboard "needs attention" badge) and counts the instance
# unhealthy until boot-complete clears its own marker on a later run.
#
# Part 1 runs the REAL boot-complete.sh against a fake `waydroid` (test
# seams JERUSALEM_DATA_DIR / BOOT_COMPLETE_OPT). Part 2 runs the REAL
# df-watchdog.sh against a fake docker/curl/systemctl (same harness as the
# keepalive-v2 suite). Hermetic. Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
BC_SRC="$HERE/../../waydroid-image/boot-complete.sh"
WD_SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export T

echo "== bash -n =="
bash -n "$BC_SRC" && ok "boot-complete.sh syntax" || bad "boot-complete.sh syntax"
bash -n "$WD_SRC" && ok "df-watchdog.sh syntax" || bad "df-watchdog.sh syntax"

# ---------- Part 1: boot-complete.sh install marker ----------
BIN="$T/fakebin"; mkdir -p "$BIN"
cat > "$BIN/waydroid" <<'EOF'
#!/bin/sh
# scenario from $T/wd_scenario: installed | ok | fail | timeout
sc=$(cat "$T/wd_scenario" 2>/dev/null || echo ok)
case "$*" in
  *"pm list packages"*)
    if [ "$sc" = "installed" ]; then echo "package:/data/app/com.roblox.client.apk"; fi
    exit 0 ;;
  *"pm install"*)
    case "$sc" in
      ok) echo "Success"; exit 0 ;;
      fail)
        printf 'Failure [INSTALL_FAILED_INVALID_APK: Failed to extract native libraries, res=-2]\nAdditional detail line that must not leak into the marker\n'
        exit 1 ;;
      timeout) sleep 30; exit 0 ;;
    esac
    exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$BIN/waydroid"

bc_setup() { # fresh data dir + opt dir with fake APKs
    rm -rf "$T/S" "$T/opt"; mkdir -p "$T/S" "$T/opt"
    echo "fake-apk" > "$T/opt/roblox-base.apk"
    echo "fake-apk" > "$T/opt/roblox-x86_64.apk"
    : > "$T/bc.out"
}
bc_run() { # bc_run <scenario> [install_timeout_secs]
    echo "$1" > "$T/wd_scenario"
    if [ -n "${2:-}" ]; then export BC_PM_INSTALL_TIMEOUT_S="$2"; else unset BC_PM_INSTALL_TIMEOUT_S; fi
    JERUSALEM_DATA_DIR="$T/S" BOOT_COMPLETE_OPT="$T/opt" \
      PATH="$BIN:/usr/bin:/bin" sh "$BC_SRC" > "$T/bc.out" 2>&1
    unset BC_PM_INSTALL_TIMEOUT_S
}

echo "== B1: already installed -> roblox_done, no failed marker =="
bc_setup; bc_run installed
[ -f "$T/S/roblox_done" ] && ok "B1 roblox_done written" || bad "B1 no roblox_done"
[ ! -f "$T/S/roblox_install_failed" ] && ok "B1 no failed marker" || bad "B1 spurious failed marker"

echo "== B2: install Success -> roblox_done, no failed marker =="
bc_setup; bc_run ok
[ -f "$T/S/roblox_done" ] && ok "B2 roblox_done written" || bad "B2 no roblox_done"
[ ! -f "$T/S/roblox_install_failed" ] && ok "B2 no failed marker" || bad "B2 spurious failed marker"

echo "== B3: pm install fails -> marker with first-line reason, no roblox_done =="
bc_setup; bc_run fail
[ -f "$T/S/roblox_install_failed" ] && ok "B3 marker written" || bad "B3 no marker"
[ "$(head -1 "$T/S/roblox_install_failed")" = "pm install failed: Failure [INSTALL_FAILED_INVALID_APK: Failed to extract native libraries, res=-2]" ] \
  && ok "B3 reason is pm first line" || bad "B3 reason wrong: $(head -1 "$T/S/roblox_install_failed" 2>/dev/null)"
[ ! -f "$T/S/roblox_done" ] && ok "B3 no roblox_done" || bad "B3 roblox_done on failure"
grep -q "must not leak" "$T/S/roblox_install_failed" \
  && bad "B3 second pm line leaked into marker" || ok "B3 only first line in marker"

echo "== B4: pm install times out -> marker names the timeout =="
bc_setup; bc_run timeout 1
[ -f "$T/S/roblox_install_failed" ] && ok "B4 marker written" || bad "B4 no marker"
grep -q "timed out after 1s" "$T/S/roblox_install_failed" \
  && ok "B4 reason names timeout" || bad "B4 reason wrong: $(head -1 "$T/S/roblox_install_failed" 2>/dev/null)"
[ ! -f "$T/S/roblox_done" ] && ok "B4 no roblox_done" || bad "B4 roblox_done on timeout"

echo "== B5: APKs missing from image -> marker, no install attempted =="
bc_setup; rm -f "$T/opt/roblox-base.apk" "$T/opt/roblox-x86_64.apk"; bc_run ok
[ -f "$T/S/roblox_install_failed" ] && ok "B5 marker written" || bad "B5 no marker"
grep -q "APKs missing" "$T/S/roblox_install_failed" \
  && ok "B5 reason names missing APKs" || bad "B5 reason wrong: $(head -1 "$T/S/roblox_install_failed" 2>/dev/null)"
[ ! -f "$T/S/roblox_done" ] && ok "B5 no roblox_done" || bad "B5 roblox_done without APKs"

echo "== B6: failed run then successful run -> marker cleared, roblox_done =="
bc_setup; bc_run fail
[ -f "$T/S/roblox_install_failed" ] && ok "B6 marker after failed run" || bad "B6 no marker after fail"
bc_run installed   # same $T/S: package now present, install skipped
[ ! -f "$T/S/roblox_install_failed" ] && ok "B6 marker cleared on recovery" || bad "B6 marker stuck"
[ -f "$T/S/roblox_done" ] && ok "B6 roblox_done on recovery" || bad "B6 no roblox_done"

# ---------- Part 2: watchdog 3c mirroring (fake docker, like keepalive-v2) ----------
WBIN="$T/wbin"; WST="$T/wst"; WVOL="$T/wvol"; WLOG="$T/wdog.log"; CALLS="$T/wcalls"
mkdir -p "$WBIN" "$WST" "$WVOL"
export CALLS
cat > "$WBIN/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
sub=$1; shift
case "$sub" in
  ps)      cat "$T/docker_ps" 2>/dev/null ;;
  inspect) date -u +%FT%TZ ;;
  start|restart) exit 0 ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep) exit 0 ;;   # all helpers alive
      waydroid) # v9 session probe: `waydroid shell getprop sys.boot_completed`
        case "$*" in *getprop*) echo 1; exit 0;; esac
        exit 0 ;;        # dumpsys package -> empty (version-pin skips)
      *) exit 0 ;;
    esac ;;
esac
exit 0
EOF
cat > "$WBIN/curl" <<'EOF'
#!/bin/bash
echo 200; exit 0
EOF
cat > "$WBIN/systemctl" <<'EOF'
#!/bin/bash
case "$1" in is-active) exit 0;; start|restart) exit 0;; esac
exit 0
EOF
chmod +x "$WBIN"/*
w_setup() {
    rm -rf "$WST" "$WVOL"; mkdir -p "$WST" "$WVOL"
    : > "$CALLS"; : > "$WLOG"
    echo "wd-rbx1" > "$T/docker_ps"
    mkdir -p "$WVOL/wd-rbx1-data/_data/data/media/0/jerusalem"
    touch "$WVOL/wd-rbx1-data/_data/data/media/0/jerusalem/booted"
    touch "$WVOL/wd-rbx1-data/_data/data/media/0/jerusalem/live.png"
}
w_base() { echo "$WVOL/wd-rbx1-data/_data/data/media/0/jerusalem"; }
w_run() {
    DF_ST="$WST" DF_LOG="$WLOG" DF_INSTANCES="rbx1" DF_VOLBASE="$WVOL" \
      DF_LOCKDIR="$WST/lock" \
      PATH="$WBIN:/usr/bin:/bin" bash "$WD_SRC" >/dev/null 2>&1
}
degraded_lines() { grep -c "DEGRADED" "$WLOG" 2>/dev/null; }

echo "== W1: roblox_install_failed -> instance marker + single DEGRADED line =="
w_setup
printf 'pm install failed: Failure [INSTALL_FAILED_INVALID_APK]\n' > "$(w_base)/roblox_install_failed"
w_run
[ -f "$WST/rbx1.degraded" ] && ok "W1 instance marker created" || bad "W1 no marker"
head -1 "$WST/rbx1.degraded" | grep -q "^Roblox install failed: pm install failed: Failure" \
  && ok "W1 reason mirrored" || bad "W1 reason: $(head -1 "$WST/rbx1.degraded" 2>/dev/null)"
[ "$(degraded_lines)" = 1 ] && ok "W1 one DEGRADED log line" || bad "W1 log lines=$(degraded_lines)"

echo "== W2: rerun with same marker -> no log spam, marker kept =="
before=$(degraded_lines)
w_run
[ "$(degraded_lines)" = "$before" ] && ok "W2 no repeat DEGRADED line" || bad "W2 log spam"
[ -f "$WST/rbx1.degraded" ] && ok "W2 marker kept" || bad "W2 marker dropped"

echo "== W3: pre-existing circuit-breaker marker is never clobbered =="
w_setup
printf 'container restart storm (test)\n' > "$WST/rbx1.degraded"
printf 'pm install failed: Failure [X]\n' > "$(w_base)/roblox_install_failed"
before=$(degraded_lines)
w_run
[ "$(head -1 "$WST/rbx1.degraded")" = "container restart storm (test)" ] \
  && ok "W3 pre-existing marker kept" || bad "W3 clobbered: $(head -1 "$WST/rbx1.degraded" 2>/dev/null)"
[ "$(degraded_lines)" = "$before" ] && ok "W3 no new DEGRADED line" || bad "W3 log spam"

echo "== W4: no install marker -> 3c stays silent =="
w_setup
w_run
[ ! -f "$WST/rbx1.degraded" ] && ok "W4 no marker created" || bad "W4 spurious marker"

echo "== W5: install marker gone (boot-complete recovered) -> auto-clear =="
w_setup
printf 'Roblox install failed: pm install failed: Failure [X]\n' > "$WST/rbx1.degraded"
w_run   # failed marker absent, everything else healthy -> section 6 clears
[ ! -f "$WST/rbx1.degraded" ] && ok "W5 degraded auto-cleared" || bad "W5 marker stuck"
grep -q "recovered from DEGRADED" "$WLOG" \
  && ok "W5 recovered log line" || bad "W5 no recovered line"

echo "== W6: empty install marker -> 'unknown error' default, never blank =="
w_setup
: > "$(w_base)/roblox_install_failed"
w_run
head -1 "$WST/rbx1.degraded" | grep -q "^Roblox install failed: unknown error$" \
  && ok "W6 default reason" || bad "W6 reason: $(head -1 "$WST/rbx1.degraded" 2>/dev/null)"

echo "== W7: keepalive.degraded alone -> keepalive reason, no install cross-talk =="
w_setup
printf 'launch attempts over budget\n' > "$(w_base)/keepalive.degraded"
w_run
head -1 "$WST/rbx1.degraded" | grep -q "^Roblox keepalive:" \
  && ok "W7 keepalive reason kept" || bad "W7 reason: $(head -1 "$WST/rbx1.degraded" 2>/dev/null)"
head -1 "$WST/rbx1.degraded" | grep -q "install failed" \
  && bad "W7 install cross-talk" || ok "W7 no install cross-talk"

echo ""
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
