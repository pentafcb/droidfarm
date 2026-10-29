#!/bin/bash
# test_cron_ensure.sh — hermetic tests for the cron-ensure package:
#   1. install.sh's ensure_cron() (verbatim-extracted between the
#      cron-ensure markers) — the fresh-install path.
#   2. baseline-newbox.sh's ensure_cron_daemon() (verbatim-extracted between
#      the cron-ensure-fn markers) — the new-box baseline path.
#   3. verify-live.sh's CRON_DAEMON probe wiring (contract checks).
#
# Everything runs against fake apt-get/systemctl/service/crontab binaries;
# the real scripts are never executed and nothing on this host is touched.
#
# Hermeticity note: the functions only apt-get install when the crontab
# binary is MISSING, so test envs with a fake crontab on PATH (like the
# newbox-baseline suite's) can never escape to a real apt-get.
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TPASS=0; TFAIL=0
tpass() { TPASS=$((TPASS+1)); }
tfail() { TFAIL=$((TFAIL+1)); echo "TEST-FAIL: $*"; }

extract_block() { # file begin-marker end-marker
    awk -v b="$2" -v e="$3" '$0==b{f=1;next} $0==e{f=0} f' "$1"
}

# --- fake host -------------------------------------------------------------
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; BIN_CRONTAB="$T/bin-crontab"; BIN_NOSYS="$T/bin-nosys"
mkdir -p "$BIN" "$BIN_CRONTAB" "$BIN_NOSYS"
: > "$T/calls"

# fake systemctl: is-active cron honors FAKE_CRON_STATE unless $T/daemon-up
# exists (created by a successful fake enable); every invocation is logged.
mk_systemctl() { # dir
    cat > "$1/systemctl" <<EOF
#!/bin/bash
echo "systemctl \$*" >> "$T/calls"
if [ "\$1" = "is-active" ]; then
    if [ -f "$T/daemon-up" ]; then echo "active"; else echo "\${FAKE_CRON_STATE:-inactive}"; fi
    exit 0
fi
if [ "\$1" = "enable" ]; then
    [ "\${FAKE_ENABLE_WORKS:-1}" = "1" ] && : > "$T/daemon-up"
    exit 0
fi
exit 0
EOF
    chmod +x "$1/systemctl"
}
mk_apt() { # dir
    cat > "$1/apt-get" <<EOF
#!/bin/bash
echo "apt-get \$*" >> "$T/calls"
exit \${FAKE_APT_RC:-0}
EOF
    chmod +x "$1/apt-get"
}
mk_service() { # dir
    cat > "$1/service" <<EOF
#!/bin/bash
echo "service \$*" >> "$T/calls"
exit 0
EOF
    chmod +x "$1/service"
}
mk_crontab() { # dir
    printf '#!/bin/bash\nexit 0\n' > "$1/crontab"; chmod +x "$1/crontab"
}
mk_systemctl "$BIN";          mk_apt "$BIN";          mk_service "$BIN"
mk_systemctl "$BIN_CRONTAB";  mk_apt "$BIN_CRONTAB";  mk_service "$BIN_CRONTAB"; mk_crontab "$BIN_CRONTAB"
                              mk_apt "$BIN_NOSYS";    mk_service "$BIN_NOSYS"   # no systemctl, no crontab

reset_calls() { : > "$T/calls"; rm -f "$T/daemon-up"; }
calls_have() { grep -q -- "$1" "$T/calls" && tpass "$2" || tfail "$2 (calls: $(tr '\n' '|' < "$T/calls"))"; }
calls_lack() { grep -q -- "$1" "$T/calls" && tfail "$2 (unexpected call: $1)" || tpass "$2"; }

# --- 1. install.sh's ensure_cron -------------------------------------------
I_FN="$(extract_block "$REPO/install.sh" '# --- cron-ensure-begin ---' '# --- cron-ensure-end ---')"
[ -n "$I_FN" ] && tpass "install.sh: cron-ensure block extracted" || tfail "install.sh: cron-ensure block missing"
echo "$I_FN" | grep -q '^ensure_cron()' && tpass "install.sh: ensure_cron defined" || tfail "install.sh: ensure_cron not defined"
eval "$I_FN"

# I1: cron already active -> fast no-op, apt-get never called
reset_calls
export FAKE_CRON_STATE=active FAKE_APT_RC=0 FAKE_ENABLE_WORKS=1
OUT="$( ( PATH="$BIN" ensure_cron ) 2>&1 )"; RC=$?
[ "$RC" = 0 ] && tpass "I1 rc=0 when cron active" || tfail "I1 rc=$RC"
echo "$OUT" | grep -q "cron already active" && tpass "I1 already-active message" || tfail "I1 no already-active message: $OUT"
calls_lack "apt-get" "I1 apt-get not called when active"
calls_lack "systemctl enable" "I1 enable not called when active"

# I2: cron inactive, crontab binary missing -> apt-get install + enable
reset_calls
export FAKE_CRON_STATE=inactive FAKE_APT_RC=0 FAKE_ENABLE_WORKS=1
OUT="$( ( PATH="$BIN" ensure_cron ) 2>&1 )"; RC=$?
[ "$RC" = 0 ] && tpass "I2 rc=0" || tfail "I2 rc=$RC"
calls_have "apt-get install -y -qq cron" "I2 apt-get install cron called"
calls_have "systemctl enable --now cron" "I2 systemctl enable --now cron called"

# I2b: cron inactive but crontab binary present -> package already installed,
# only the daemon is (re)started; apt-get never called
reset_calls
export FAKE_CRON_STATE=inactive FAKE_APT_RC=0 FAKE_ENABLE_WORKS=1
OUT="$( ( PATH="$BIN_CRONTAB" ensure_cron ) 2>&1 )"; RC=$?
[ "$RC" = 0 ] && tpass "I2b rc=0" || tfail "I2b rc=$RC"
calls_lack "apt-get" "I2b apt-get not called when crontab exists"
calls_have "systemctl enable --now cron" "I2b enable called when crontab exists"

# I3: apt-get fails -> WARNING printed, enable still attempted, rc stays 0
# (a failed ensure must never abort the install under set -e)
reset_calls
export FAKE_CRON_STATE=inactive FAKE_APT_RC=1 FAKE_ENABLE_WORKS=1
OUT="$( ( PATH="$BIN" ensure_cron ) 2>&1 )"; RC=$?
[ "$RC" = 0 ] && tpass "I3 rc=0 despite apt failure" || tfail "I3 rc=$RC"
echo "$OUT" | grep -q "WARNING: could not install" && tpass "I3 install-failure warning" || tfail "I3 no warning: $OUT"
calls_have "systemctl enable --now cron" "I3 enable still attempted after apt failure"

# I4: no systemctl on PATH -> service(8) fallback
reset_calls
export FAKE_CRON_STATE=inactive FAKE_APT_RC=0
OUT="$( ( PATH="$BIN_NOSYS" ensure_cron ) 2>&1 )"; RC=$?
[ "$RC" = 0 ] && tpass "I4 rc=0 without systemctl" || tfail "I4 rc=$RC"
calls_have "service cron start" "I4 service fallback used"
calls_lack "systemctl" "I4 systemctl not touched"

# I5: wiring — the call site lives in step 7 after the block; bash -n clean
awk '/# --- cron-ensure-end ---/{e=1} e && /^ensure_cron$/{c=1} END{exit !c}' "$REPO/install.sh" \
    && tpass "I5 ensure_cron called in step 7" || tfail "I5 call site misplaced"
bash -n "$REPO/install.sh" && tpass "I5 install.sh bash -n clean" || tfail "I5 install.sh bash -n failed"

# --- 2. baseline-newbox.sh's ensure_cron_daemon -------------------------------
B_FN="$(extract_block "$REPO/pending-deploy/newbox-baseline/baseline-newbox.sh" '# --- cron-ensure-fn-begin ---' '# --- cron-ensure-fn-end ---')"
[ -n "$B_FN" ] && tpass "baseline: cron-ensure-fn block extracted" || tfail "baseline: block missing"
# Stubs for the extracted function's ok/warn helpers. They must write to
# FILES: the function under test runs in a subshell, so shell variables
# assigned inside would be lost when it exits.
: > "$T/ok.log"; : > "$T/warn.log"
ok()   { echo "OK: $1" >> "$T/ok.log"; }
warn() { echo "WARN: $1" >> "$T/warn.log"; }
reset_marks() { : > "$T/ok.log"; : > "$T/warn.log"; }
ok_has()     { grep -q -- "$1" "$T/ok.log" && tpass "$2" || tfail "$2 (ok.log: $(tr '\n' '|' < "$T/ok.log"))"; }
ok_lack()    { grep -q -- "$1" "$T/ok.log" && tfail "$2 (unexpected ok: $1)" || tpass "$2"; }
warn_has()   { grep -q -- "$1" "$T/warn.log" && tpass "$2" || tfail "$2 (warn.log: $(tr '\n' '|' < "$T/warn.log"))"; }
warn_empty() { [ ! -s "$T/warn.log" ] && tpass "$1" || tfail "$1 (warn.log: $(tr '\n' '|' < "$T/warn.log"))"; }
eval "$B_FN"

# B1: cron already active -> ok recorded, apt-get never called, no warnings
reset_calls; reset_marks
export FAKE_CRON_STATE=active FAKE_APT_RC=0 FAKE_ENABLE_WORKS=1
( PATH="$BIN" ensure_cron_daemon )
ok_has "cron daemon active" "B1 ok recorded"
calls_lack "apt-get" "B1 apt-get not called when active"
warn_empty "B1 no warnings"

# B2: inactive, crontab missing -> apt-get update+install, enable, active
reset_calls; reset_marks
export FAKE_CRON_STATE=inactive FAKE_APT_RC=0 FAKE_ENABLE_WORKS=1
( PATH="$BIN" ensure_cron_daemon )
calls_have "apt-get update" "B2 apt-get update called"
calls_have "apt-get install -y -qq cron" "B2 apt-get install cron called"
ok_has "installed the cron package" "B2 install ok recorded"
ok_has "cron daemon enabled and active" "B2 enabled-active ok recorded"

# B2b: inactive but crontab present -> package already there, only enable;
# apt-get never called (this is the hermetic-escape guard: envs with a fake
# crontab but no fake apt-get must never reach a real apt-get)
reset_calls; reset_marks
export FAKE_CRON_STATE=inactive FAKE_APT_RC=0 FAKE_ENABLE_WORKS=1
( PATH="$BIN_CRONTAB" ensure_cron_daemon )
calls_lack "apt-get" "B2b apt-get not called when crontab exists"
calls_have "systemctl enable --now cron" "B2b enable called when crontab exists"
ok_lack "installed the cron package" "B2b no install ok recorded"
ok_has "cron daemon enabled and active" "B2b enabled-active ok recorded"

# B3: apt fails AND enable fails -> warnings only, never a hard failure
reset_calls; reset_marks
export FAKE_CRON_STATE=inactive FAKE_APT_RC=1 FAKE_ENABLE_WORKS=0
( PATH="$BIN" ensure_cron_daemon ); RC=$?
[ "$RC" = 0 ] && tpass "B3 rc=0 (warn-only)" || tfail "B3 rc=$RC"
warn_has "could not install the cron package" "B3 install-failure warning"
warn_has "will not fire until cron is running" "B3 dead-daemon warning"
calls_have "systemctl enable --now cron" "B3 enable attempted despite apt failure"

# B4: no systemctl on PATH -> warning, no crash
reset_calls; reset_marks
export FAKE_APT_RC=0
( PATH="$BIN_NOSYS" ensure_cron_daemon ); RC=$?
[ "$RC" = 0 ] && tpass "B4 rc=0 without systemctl" || tfail "B4 rc=$RC"
warn_has "systemctl not found" "B4 no-systemctl warning"

# B5: wiring — call site after the block, header documents 1b, bash -n clean
awk '/# --- cron-ensure-fn-end ---/{e=1} e && /^ensure_cron_daemon$/{c=1} END{exit !c}' \
        "$REPO/pending-deploy/newbox-baseline/baseline-newbox.sh" \
    && tpass "B5 ensure_cron_daemon called as step 1b" || tfail "B5 call site misplaced"
grep -q "1b. ensure the cron daemon" "$REPO/pending-deploy/newbox-baseline/baseline-newbox.sh" \
    && tpass "B5 header documents 1b" || tfail "B5 header not updated"
bash -n "$REPO/pending-deploy/newbox-baseline/baseline-newbox.sh" \
    && tpass "B5 baseline bash -n clean" || tfail "B5 baseline bash -n failed"

# --- 3. verify-live.sh CRON_DAEMON probe wiring -------------------------------
grep -q 'echo "CRON_DAEMON=' "$REPO/pending-deploy/live-verify/verify-live.sh" \
    && tpass "V1 remote CRON_DAEMON probe present" || tfail "V1 remote probe missing"
grep -q 'ok "cron daemon active"' "$REPO/pending-deploy/live-verify/verify-live.sh" \
    && tpass "V2 active branch present" || tfail "V2 active branch missing"
grep -q 'the minute-cron watchdog and the backup cron never fire' "$REPO/pending-deploy/live-verify/verify-live.sh" \
    && tpass "V3 warn branch names the impact" || tfail "V3 warn branch missing"

echo
echo "$TPASS passed, $TFAIL failed"
[ "$TFAIL" -eq 0 ]
