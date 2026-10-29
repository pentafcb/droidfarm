#!/bin/bash
# test_install_baseline.sh — hermetic tests for the install.sh
# install-baseline block (fresh installs got a dashboard but no watchdog
# minute-cron and no daily backup cron; install.sh now reuses
# baseline-newbox.sh warn-only).
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="$REPO/install.sh"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1"; }

# --- extract the block verbatim (same delimiters as the product) ---
BLOCK="$(awk '/# --- install-baseline-begin ---/{f=1;next}/# --- install-baseline-end ---/{f=0}f' "$INSTALL")"

# T1: delimiters present exactly once each, block non-empty
[ "$(grep -c '# --- install-baseline-begin ---' "$INSTALL")" = "1" ] && ok || bad "begin delimiter count"
[ "$(grep -c '# --- install-baseline-end ---' "$INSTALL")" = "1" ] && ok || bad "end delimiter count"
[ -n "$BLOCK" ] && ok || bad "block non-empty"

# Load the extracted function into this shell
eval "$BLOCK"
type install_baseline >/dev/null 2>&1 && ok || bad "function loads from extracted block"

# --- fake repo tree ---
setup_fake() {
    FAKE="$(mktemp -d)"
    mkdir -p "$FAKE/pending-deploy/newbox-baseline" "$FAKE/waydroid-image" "$FAKE/pending-deploy/backup-state"
    : > "$FAKE/waydroid-image/df-watchdog.sh"
    : > "$FAKE/pending-deploy/backup-state/backup-state.sh"
    : > "$FAKE/pending-deploy/backup-state/verify-backup.sh"
    cat > "$FAKE/pending-deploy/newbox-baseline/baseline-newbox.sh" <<'EOF'
#!/bin/bash
echo "$1|$2|$3" >> "$ARGV_LOG"
exit "${BASELINE_RC:-0}"
EOF
    chmod +x "$FAKE/pending-deploy/newbox-baseline/baseline-newbox.sh"
    export ARGV_LOG="$FAKE/argv.log"
}

# T2: happy path — baseline runs with the 3 canonical sources in order
setup_fake
OUT="$(install_baseline "$FAKE" 2>&1)"; RC=$?
[ "$RC" = "0" ] && ok || bad "happy path exit 0 (rc=$RC)"
[ "$(cat "$FAKE/argv.log")" = "$FAKE/waydroid-image/df-watchdog.sh|$FAKE/pending-deploy/backup-state/backup-state.sh|$FAKE/pending-deploy/backup-state/verify-backup.sh" ] \
    && ok || bad "baseline argv order (got: $(cat "$FAKE/argv.log" 2>/dev/null))"
echo "$OUT" | grep -q "WARNING" && bad "happy path warns" || ok
rm -rf "$FAKE"

# T3: baseline fails -> WARNING, still exit 0 (never aborts under set -e)
setup_fake
OUT="$(BASELINE_RC=1 install_baseline "$FAKE" 2>&1)"; RC=$?
[ "$RC" = "0" ] && ok || bad "failing baseline still exit 0 (rc=$RC)"
echo "$OUT" | grep -q "WARNING" && ok || bad "failing baseline warns"
SURV="$(bash -e -c 'eval "$1"; BASELINE_RC=1 install_baseline "$2" >/dev/null 2>&1; echo SURVIVED' _ "$BLOCK" "$FAKE" 2>&1)"
[ "$SURV" = "SURVIVED" ] && ok || bad "set -e survival on baseline failure (got: $SURV)"
rm -rf "$FAKE"

# T4: baseline script missing -> warn + skip, never invoked
setup_fake
rm "$FAKE/pending-deploy/newbox-baseline/baseline-newbox.sh"
OUT="$(install_baseline "$FAKE" 2>&1)"; RC=$?
[ "$RC" = "0" ] && ok || bad "missing baseline exit 0"
echo "$OUT" | grep -q "WARNING" && ok || bad "missing baseline warns"
[ ! -f "$FAKE/argv.log" ] && ok || bad "missing baseline never invoked"
rm -rf "$FAKE"

# T5: one source missing (verify-backup.sh) -> warn + skip, baseline untouched
setup_fake
rm "$FAKE/pending-deploy/backup-state/verify-backup.sh"
OUT="$(install_baseline "$FAKE" 2>&1)"; RC=$?
[ "$RC" = "0" ] && ok || bad "missing source exit 0"
echo "$OUT" | grep -q "WARNING" && ok || bad "missing source warns"
[ ! -f "$FAKE/argv.log" ] && ok || bad "missing source never runs baseline"
rm -rf "$FAKE"

# T6: no apostrophes in the block (bash -c single-quote wrap safety)
echo "$BLOCK" | grep -q "'" && bad "apostrophe in block" || ok

# T7: no legacy refs in the block (audit check-6 hygiene)
echo "$BLOCK" | grep -qE 'droidfarm\.service|/root/droidfarm|209\.222\.10\.93' && bad "legacy ref in block" || ok

# T8: call site — one invocation outside the block, step labels renumbered
CALLS="$(grep -c '^install_baseline$' "$INSTALL")"
[ "$CALLS" = "1" ] && ok || bad "call-site count (got $CALLS)"
grep -q '\[8/9\] Installing host baseline' "$INSTALL" && ok || bad "step 8/9 label"
grep -q '\[9/9\] Starting Jerusalem' "$INSTALL" && ok || bad "step 9/9 label"
grep -q '\[7/9\] Installing log trimmer' "$INSTALL" && ok || bad "step 7/9 label"

# T9: bash -n on install.sh
bash -n "$INSTALL" && ok || bad "bash -n install.sh"

# T10: the 4 canonical source paths are the ones passed
echo "$BLOCK" | grep -q 'pending-deploy/newbox-baseline/baseline-newbox.sh' && ok || bad "baseline source path"
echo "$BLOCK" | grep -q 'waydroid-image/df-watchdog.sh' && ok || bad "watchdog source path"
echo "$BLOCK" | grep -q 'pending-deploy/backup-state/backup-state.sh' && ok || bad "backup-state source path"
echo "$BLOCK" | grep -q 'pending-deploy/backup-state/verify-backup.sh' && ok || bad "verify-backup source path"

echo "$PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
