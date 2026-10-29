#!/bin/bash
# test_newbox_sync.sh — hermetic tests for pending-deploy/newbox-sync/sync-newbox.sh
#
# Fake deploy-all.sh / scp / ssh / verify-live.sh record every invocation to a
# call log; failure injection via FAKE_*_RC and FAKE_SSH_FAIL_ON (1-based ssh
# call number: 1=mkdir, 2=baseline run). The 4 baseline sources are the REAL
# repo files (read-only). No network, no VPS.
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SYNC="$REPO/pending-deploy/newbox-sync/sync-newbox.sh"
PASS=0; FAILC=0
ok()  { PASS=$((PASS+1)); }
bad() { FAILC=$((FAILC+1)); echo "TEST-FAIL: $*"; }

mktest() {
    T="$(mktemp -d)"
    BIN="$T/bin"; mkdir -p "$BIN"
    CALLLOG="$T/calls.log"; : > "$CALLLOG"; rm -f "$CALLLOG.n"
    export CALLLOG
    cat > "$BIN/fake-deploy-all.sh" <<'EOF'
#!/bin/bash
echo "deploy-all $*" >> "$CALLLOG"
[ "${FAKE_DEPLOY_RC:-0}" = "0" ] || exit 1
echo "fake deploy-all ok for $1"
exit 0
EOF
    chmod +x "$BIN/fake-deploy-all.sh"
    cat > "$BIN/fake-scp.sh" <<'EOF'
#!/bin/bash
echo "scp $*" >> "$CALLLOG"
[ "${FAKE_SCP_RC:-0}" = "0" ] || exit 1
exit 0
EOF
    chmod +x "$BIN/fake-scp.sh"
    cat > "$BIN/fake-ssh.sh" <<'EOF'
#!/bin/bash
N="$(cat "$CALLLOG.n" 2>/dev/null || echo 0)"; N=$((N+1)); echo "$N" > "$CALLLOG.n"
echo "ssh[$N] $*" >> "$CALLLOG"
if [ -n "${FAKE_SSH_FAIL_ON:-}" ] && [ "$N" = "$FAKE_SSH_FAIL_ON" ]; then exit 1; fi
exit 0
EOF
    chmod +x "$BIN/fake-ssh.sh"
    cat > "$BIN/fake-verify.sh" <<'EOF'
#!/bin/bash
echo "verify-live $*" >> "$CALLLOG"
[ "${FAKE_VERIFY_RC:-0}" = "0" ] || exit 1
echo "fake verify-live ok"
exit 0
EOF
    chmod +x "$BIN/fake-verify.sh"
    unset FAKE_DEPLOY_RC FAKE_SCP_RC FAKE_SSH_FAIL_ON FAKE_VERIFY_RC STDIN_DATA
    # export-but-unset: children see them unset (-> :- defaults), while a
    # plain assignment in a test propagates to the fakes automatically.
    export FAKE_DEPLOY_RC FAKE_SCP_RC FAKE_SSH_FAIL_ON FAKE_VERIFY_RC
}

# run_sync <args...>: runs the orchestrator with fakes; prints rc; output in $T/out.log
run_sync() {
    SYNC_DEPLOY_ALL="$BIN/fake-deploy-all.sh" \
    SYNC_VERIFY_LIVE="$BIN/fake-verify.sh" \
    DF_SCP="$BIN/fake-scp.sh" DF_SSH="$BIN/fake-ssh.sh" \
    SSH_OPTS="-o BatchMode=yes" \
    bash "$SYNC" "$@" < "${STDIN_DATA:-/dev/null}" > "$T/out.log" 2>&1
    echo $?
}

# --- 1. default host ---------------------------------------------------------
mktest
rc="$(run_sync --yes)"
[ "$rc" = "0" ] && ok "T1 happy path exit 0" || bad "T1 happy path rc=$rc"
grep -q '^deploy-all root@64.177.49.68 --yes' "$CALLLOG" \
    && ok "T1 default host to deploy-all.sh" || bad "T1 default host not passed to deploy-all.sh"
grep -q 'DONE: new VPS root@64.177.49.68' "$T/out.log" \
    && ok "T1 DONE line" || bad "T1 DONE line missing"

# --- 2. custom host passthrough ----------------------------------------------
mktest
rc="$(run_sync deploy@10.0.0.5 --yes)"
[ "$rc" = "0" ] && ok "T2 exit 0" || bad "T2 rc=$rc"
grep -q '^deploy-all deploy@10.0.0.5 --yes' "$CALLLOG" \
    && ok "T2 custom host passthrough" || bad "T2 custom host not passed through"

# --- 3. --yes skips the prompt (stdin /dev/null would abort if prompted) -----
mktest
rc="$(run_sync --yes)"
[ "$rc" = "0" ] && ok "T3 exit 0 with closed stdin" || bad "T3 rc=$rc (prompt must be skipped)"
grep -q 'Proceed?' "$T/out.log" && bad "T3 prompt shown despite --yes" || ok "T3 no prompt with --yes"

# --- 4. prompt: n aborts, y proceeds ------------------------------------------
mktest
STDIN_DATA="$T/no.txt"; printf 'n\n' > "$STDIN_DATA"
rc="$(run_sync)"
[ "$rc" = "1" ] && ok "T4 'n' aborts" || bad "T4 'n' rc=$rc"
grep -q '^deploy-all' "$CALLLOG" && bad "T4 deploy-all ran after abort" || ok "T4 nothing ran after abort"
mktest
STDIN_DATA="$T/yes.txt"; printf 'y\n' > "$STDIN_DATA"
rc="$(run_sync)"
[ "$rc" = "0" ] && ok "T4 'y' proceeds" || bad "T4 'y' rc=$rc"
grep -q '^deploy-all root@64.177.49.68 --yes' "$CALLLOG" \
    && ok "T4 'y' reaches deploy-all.sh" || bad "T4 'y' did not reach deploy-all.sh"

# --- 5. order: deploy-all -> ssh(mkdir) -> scp -> ssh(baseline) -> verify ----
mktest
rc="$(run_sync --yes)"
[ "$rc" = "0" ] && ok "T5 exit 0" || bad "T5 rc=$rc"
seq="$(grep -o '^deploy-all\|^ssh\[1\]\|^scp\|^ssh\[2\]\|^verify-live' "$CALLLOG" | tr '\n' ' ')"
[ "$seq" = "deploy-all ssh[1] scp ssh[2] verify-live " ] \
    && ok "T5 step order" || bad "T5 wrong order: $seq"

# --- 6. deploy-all failure: abort, nothing else runs --------------------------
mktest
FAKE_DEPLOY_RC=1
rc="$(run_sync --yes)"
[ "$rc" = "1" ] && ok "T6 exit 1" || bad "T6 rc=$rc"
grep -q '^scp\|^ssh\|^verify-live' "$CALLLOG" \
    && bad "T6 scp/ssh/verify ran after deploy-all failure" || ok "T6 nothing else ran"
grep -q 'step1 code-sync=FAILED' "$T/out.log" \
    && ok "T6 summary names step 1" || bad "T6 summary does not name step 1"

# --- 7. ssh mkdir failure: abort before scp -----------------------------------
mktest
FAKE_SSH_FAIL_ON=1
rc="$(run_sync --yes)"
[ "$rc" = "1" ] && ok "T7 exit 1" || bad "T7 rc=$rc"
grep -q '^scp' "$CALLLOG" && bad "T7 scp ran after mkdir failure" || ok "T7 scp skipped"
grep -q 'step 2 (baseline) failed' "$T/out.log" \
    && ok "T7 summary names step 2" || bad "T7 summary does not name step 2"

# --- 8. scp failure: abort before baseline run --------------------------------
mktest
FAKE_SCP_RC=1
rc="$(run_sync --yes)"
[ "$rc" = "1" ] && ok "T8 exit 1" || bad "T8 rc=$rc"
grep -q '^ssh\[2\]' "$CALLLOG" && bad "T8 baseline ssh ran after scp failure" || ok "T8 baseline ssh skipped"
grep -q '^verify-live' "$CALLLOG" && bad "T8 verify ran after scp failure" || ok "T8 verify skipped"

# --- 9. baseline run failure: abort before verify ------------------------------
mktest
FAKE_SSH_FAIL_ON=2
rc="$(run_sync --yes)"
[ "$rc" = "1" ] && ok "T9 exit 1" || bad "T9 rc=$rc"
grep -q 'baseline-newbox.sh' "$CALLLOG" \
    && ok "T9 baseline run attempted" || bad "T9 baseline run command not attempted"
grep -q '^verify-live' "$CALLLOG" && bad "T9 verify ran after baseline failure" || ok "T9 verify skipped"
grep -q 'step2 baseline=FAILED' "$T/out.log" \
    && ok "T9 summary shows step2 FAILED" || bad "T9 summary does not show step2 FAILED"

# --- 10. verify failure: exit 1, steps 1-2 ok ----------------------------------
mktest
FAKE_VERIFY_RC=1
rc="$(run_sync --yes)"
[ "$rc" = "1" ] && ok "T10 exit 1" || bad "T10 rc=$rc"
grep -q 'step1 code-sync=ok, step2 baseline=ok, step3 verify-live=FAILED' "$T/out.log" \
    && ok "T10 summary" || bad "T10 summary wrong: $(grep summary "$T/out.log")"

# --- 11. verify --host gets the BARE host (verify-live.sh prepends root@) -----
mktest
rc="$(run_sync deploy@10.0.0.5 --yes)"
[ "$rc" = "0" ] && ok "T11 exit 0" || bad "T11 rc=$rc"
grep -q '^verify-live --host=10.0.0.5 --ssh' "$CALLLOG" \
    && ok "T11 verify --host is bare" || bad "T11 verify --host not bare: $(grep ^verify-live "$CALLLOG")"

# --- 12. baseline scp carries exactly the 4 expected sources -------------------
mktest
rc="$(run_sync --yes)"
[ "$rc" = "0" ] && ok "T12 exit 0" || bad "T12 rc=$rc"
scpline="$(grep '^scp' "$CALLLOG")"
for b in baseline-newbox.sh df-watchdog.sh backup-state.sh verify-backup.sh; do
    printf '%s' "$scpline" | grep -q "$b" && ok "T12 scp carries $b" || bad "T12 scp missing source $b"
done
printf '%s' "$scpline" | grep -q 'newbox-baseline/$' \
    && ok "T12 scp target is the baseline remote dir" || bad "T12 scp target not the baseline remote dir"

# --- 13. baseline ssh runs the script with its 3 source args -------------------
mktest
rc="$(run_sync --yes)"
[ "$rc" = "0" ] && ok "T13 exit 0" || bad "T13 rc=$rc"
runline="$(grep '^ssh\[2\]' "$CALLLOG")"
printf '%s' "$runline" | grep -q 'bash.*baseline-newbox.sh.*df-watchdog.sh.*backup-state.sh.*verify-backup.sh' \
    && ok "T13 baseline run command" || bad "T13 baseline run command wrong: $runline"

# --- 14. unknown flag: usage error, nothing runs -------------------------------
mktest
rc="$(run_sync --stage-only)"
[ "$rc" = "2" ] && ok "T14 exit 2" || bad "T14 rc=$rc"
[ -s "$CALLLOG" ] && bad "T14 helpers ran on usage error" || ok "T14 nothing ran"

# --- 15. no secrets in output ---------------------------------------------------
mktest
rc="$(run_sync --yes)"
[ "$rc" = "0" ] && ok "T15 exit 0" || bad "T15 rc=$rc"
grep -qi 'password\|BEGIN.*PRIVATE KEY' "$T/out.log" \
    && bad "T15 secret-looking text in output" || ok "T15 no secrets in output"

# --- 16. bash -n clean -----------------------------------------------------------
bash -n "$SYNC" && ok "T16 bash -n clean" || bad "T16 bash -n sync-newbox.sh failed"

# --- 17. no legacy refs, no functional old-VPS hardcoding -------------------------
grep -q 'droidfarm' "$SYNC" && bad "T17 legacy 'droidfarm' ref" || ok "T17 no legacy product refs"
grep -v '^#' "$SYNC" | grep -q '209\.222\.10\.93' \
    && bad "T17 old-VPS IP in code" || ok "T17 old-VPS IP only in comments"

echo "PASS=$PASS FAIL=$FAILC"
[ "$FAILC" = "0" ]
