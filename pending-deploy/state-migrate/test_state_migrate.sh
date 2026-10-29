#!/bin/bash
# test_state_migrate.sh — hermetic tests for
# pending-deploy/state-migrate/import-state.sh
#
# Builds a fake box (fake id/systemctl on PATH, dest state dir + snapshot
# dir via env overrides) and runs the REAL script against crafted
# archives. A planted secret in the source accounts must never appear in
# any output.
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SM="$REPO/pending-deploy/state-migrate/import-state.sh"
PASS=0; FAILC=0
ok()  { PASS=$((PASS+1)); }
bad() { FAILC=$((FAILC+1)); echo "TEST-FAIL: $*"; }

# --- fixture: fake box ---------------------------------------------------------
mktree() {
    T="$(mktemp -d)"
    BIN="$T/bin"; mkdir -p "$BIN"
    printf '#!/bin/bash\necho "${FAKE_UID:-0}"\n' > "$BIN/id"; chmod +x "$BIN/id"
    cat > "$BIN/systemctl" <<EOF
#!/bin/bash
echo "\$*" >> "$T/svc-calls"
[ -f "$T/stop-fail" ] && [ "\$1" = "stop" ] && exit 1
exit 0
EOF
    chmod +x "$BIN/systemctl"
    DATA="$T/data"; mkdir -p "$DATA/batch_jobs"
    # dest starts with its OWN identity + old accounts (with a planted secret)
    for f in instances.json providers.json tokens.json roblox_login_results.json; do
        echo "{\"dest\":1}" > "$DATA/$f"
    done
    echo "desttoken" > "$DATA/static_join_token"
    echo '{"rbx1":{"username":"oldu","password":"supersecret123"}}' > "$DATA/roblox_accounts.json"
    echo '{"oldjob":{"status":"done"}}' > /dev/null
    echo '{"status":"done"}' > "$DATA/batch_jobs/$(printf '%016x' 1).json"
    chmod 600 "$DATA"/roblox_accounts.json
    # source archive builder
    SRC="$T/src"; mkdir -p "$SRC/batch_jobs"
    for f in instances.json providers.json tokens.json roblox_login_results.json roblox_accounts.json; do
        echo "{\"src\":\"$f\"}" > "$SRC/$f"
    done
    echo "srctoken" > "$SRC/static_join_token"
    echo '{"a1b2c3d4e5f60718":{"status":"done"}}' > /dev/null
    echo '{"status":"done"}' > "$SRC/batch_jobs/$(printf '%016x' 2).json"
    echo '{"status":"running"}' > "$SRC/batch_jobs/$(printf '%016x' 3).json"
    export PATH="$BIN:/usr/bin:/bin"
    export JERUSALEM_DATA="$DATA" SM_BACKUP_DIR="$T/snap"
    export SM_NO_DASHBOARD=1 SM_NO_SVC=1 SM_SERVICE=jerusalem
}
mkarch() { # $1 = out path; builds a full archive from $SRC
    # real backup-state.sh shape: bare member names, no ./ prefix
    (cd "$SRC" && tar -czf "$1" $(ls -A) 2>/dev/null)
}
run_sm() { # args passed through; captures to $OUT, rc to $RC
    OUT="$(bash "$SM" "$@" 2>&1)"; RC=$?
}
no_secret() { # assert the planted secret is nowhere in $OUT
    echo "$OUT" | grep -q "supersecret123" && bad "secret leaked: $1" || ok "no secret: $1"
}

# --- arg / preflight failures ---------------------------------------------------
mktree; run_sm
[ "$RC" = 2 ] && ok "no args -> exit 2" || bad "no args rc=$RC"
echo "$OUT" | grep -q "usage" && ok "usage printed" || bad "no usage"

mktree; run_sm --bogus
[ "$RC" = 2 ] && ok "two non-flags -> exit 2" || bad "bogus rc=$RC"

mktree
FAKE_UID=1 run_sm "/nonexistent.tar.gz"
[ "$RC" = 1 ] && ok "non-root refused" || bad "non-root rc=$RC"
echo "$OUT" | grep -q "must run as root" && ok "root message" || bad "root message missing"

mktree; run_sm "$T/nope.tar.gz"
[ "$RC" = 1 ] && ok "missing archive -> exit 1" || bad "missing rc=$RC"

mktree
echo "not a tar" > "$T/bad.tar.gz"; run_sm "$T/bad.tar.gz"
[ "$RC" = 1 ] && ok "non-tar archive -> exit 1" || bad "non-tar rc=$RC"

mktree
mkarch "$T/ok.tar.gz"
JERUSALEM_DATA="$T/nodir" run_sm "$T/ok.tar.gz"
[ "$RC" = 1 ] && ok "missing datadir -> exit 1" || bad "datadir rc=$RC"
echo "$OUT" | grep -q "state dir missing" && ok "datadir message" || bad "datadir message missing"

# --- hostile archives: fail closed ---------------------------------------------
hostile() { # $1 = label; $2 = shell fragment producing $T/h.tar.gz from $SRC/payload
    mktree
    echo x > "$SRC/payload"
    eval "$2"
    run_sm "$T/h.tar.gz"
    [ "$RC" = 1 ] && ok "hostile $1 -> exit 1" || bad "hostile $1 rc=$RC"
    echo "$OUT" | grep -q "rejected" && ok "hostile $1 named" || bad "hostile $1 not named"
    [ "$(cat "$DATA/roblox_accounts.json")" = '{"rbx1":{"username":"oldu","password":"supersecret123"}}' ] \
        && ok "hostile $1: dest untouched" || bad "hostile $1: dest touched"
    rm -f "$SRC/payload"
}
hostile "absolute path"   "tar -czf \"\$T/h.tar.gz\" -C \"\$SRC\" --transform='s,^payload\$,/etc/passwd,' payload"
hostile "dotdot member"    "tar -czf \"\$T/h.tar.gz\" -C \"\$SRC\" --transform='s,^payload\$,../evil,' payload"
hostile "dotdot nested"    "tar -czf \"\$T/h.tar.gz\" -C \"\$SRC\" --transform='s,^payload\$,batch_jobs/../../x,' payload"
hostile "unexpected file"  "(cd \"\$SRC\" && mv payload evil.sh && tar -czf \"\$T/h.tar.gz\" evil.sh)"
hostile "batch_jobs non-hex" "mkdir -p \"\$SRC/batch_jobs\" && mv \"\$SRC/payload\" \"\$SRC/batch_jobs/nothex.json\" && (cd \"\$SRC\" && tar -czf \"\$T/h.tar.gz\" batch_jobs)"

mktree
# dir-entry member "batch_jobs/" must be accepted, not rejected
(cd "$SRC" && tar -czf "$T/d.tar.gz" ./batch_jobs/) 
run_sm "$T/d.tar.gz"
[ "$RC" = 0 ] && ok "batch_jobs/ dir entry accepted" || bad "dir entry rc=$RC"

mktree
# ./-prefixed members must be accepted
(cd "$SRC" && tar -czf "$T/dot.tar.gz" ./roblox_accounts.json)
run_sm "$T/dot.tar.gz"
[ "$RC" = 0 ] && ok "./-prefixed member accepted" || bad "./ prefix rc=$RC"
echo "$OUT" | grep -q "whitelist ok" && ok "whitelist counted" || bad "whitelist line missing"

# --- corrupt JSON: fail closed, dest untouched -----------------------------------
mktree
echo "not json {" > "$SRC/roblox_accounts.json"
mkarch "$T/c.tar.gz"
run_sm "$T/c.tar.gz"
[ "$RC" = 1 ] && ok "corrupt JSON -> exit 1" || bad "corrupt rc=$RC"
[ "$(cat "$DATA/roblox_accounts.json")" = '{"rbx1":{"username":"oldu","password":"supersecret123"}}' ] \
    && ok "corrupt JSON: dest untouched" || bad "corrupt JSON: dest touched"
no_secret "corrupt run"

# --- happy path (default): portable data imported, identity preserved -----------
mktree
mkarch "$T/full.tar.gz"
run_sm "$T/full.tar.gz"
[ "$RC" = 0 ] && ok "happy path rc=0" || bad "happy rc=$RC"
[ "$(cat "$DATA/roblox_accounts.json")" = '{"src":"roblox_accounts.json"}' ] \
    && ok "accounts imported" || bad "accounts not imported"
[ "$(cat "$DATA/roblox_login_results.json")" = '{"src":"roblox_login_results.json"}' ] \
    && ok "login results imported" || bad "login results not imported"
[ -f "$DATA/batch_jobs/$(printf '%016x' 2).json" ] \
    && [ -f "$DATA/batch_jobs/$(printf '%016x' 3).json" ] \
    && ok "journals merged" || bad "journals not merged"
[ -f "$DATA/batch_jobs/$(printf '%016x' 1).json" ] \
    && ok "dest journal kept (merge, not replace)" || bad "dest journal lost"
[ "$(cat "$DATA/instances.json")" = '{"dest":1}' ] \
    && ok "instances preserved (default)" || bad "instances clobbered"
[ "$(cat "$DATA/providers.json")" = '{"dest":1}' ] \
    && ok "providers preserved (default)" || bad "providers clobbered"
[ "$(cat "$DATA/tokens.json")" = '{"dest":1}' ] \
    && [ "$(cat "$DATA/static_join_token")" = "desttoken" ] \
    && ok "tokens + join token preserved" || bad "identity clobbered"
[ "$(stat -c%a "$DATA/roblox_accounts.json")" = 600 ] && ok "file perms 600" || bad "file perms wrong"
[ "$(stat -c%a "$DATA/batch_jobs")" = 700 ] && ok "batch_jobs 700" || bad "batch_jobs perms wrong"
snap="$(ls -d "$T"/snap/pre-import-* 2>/dev/null | head -1)"
[ -n "$snap" ] && [ -f "$snap/roblox_accounts.json" ] \
    && [ "$(cat "$snap/roblox_accounts.json")" = '{"rbx1":{"username":"oldu","password":"supersecret123"}}' ] \
    && ok "snapshot holds pre-import state" || bad "snapshot missing/wrong"
[ "$(stat -c%a "$snap")" = 700 ] && ok "snapshot dir 700" || bad "snapshot dir perms"
no_secret "happy run"
[ ! -f "$T/svc-calls" ] && ok "SM_NO_SVC: systemctl untouched" || bad "systemctl called with NO_SVC"

# --- --full: instances + providers replaced, identity STILL preserved -----------
mktree
mkarch "$T/full2.tar.gz"
run_sm --full "$T/full2.tar.gz"
[ "$RC" = 0 ] && ok "--full rc=0" || bad "--full rc=$RC"
[ "$(cat "$DATA/instances.json")" = '{"src":"instances.json"}' ] \
    && [ "$(cat "$DATA/providers.json")" = '{"src":"providers.json"}' ] \
    && ok "--full imports instances+providers" || bad "--full topology not imported"
[ "$(cat "$DATA/tokens.json")" = '{"dest":1}' ] \
    && [ "$(cat "$DATA/static_join_token")" = "desttoken" ] \
    && ok "--full still preserves tokens" || bad "--full clobbered identity"
no_secret "full run"

# --- missing members: warn, never silent, import what exists --------------------
mktree
rm -f "$SRC/batch_jobs"/*.json
mkarch "$T/noj.tar.gz"
run_sm "$T/noj.tar.gz"
[ "$RC" = 0 ] && ok "no journals rc=0" || bad "no journals rc=$RC"
echo "$OUT" | grep -q "no batch_jobs journals" && ok "journal absence warned" || bad "journal warn missing"

mktree
rm -f "$SRC/roblox_accounts.json"
mkarch "$T/noa.tar.gz"
run_sm "$T/noa.tar.gz"
[ "$RC" = 0 ] && ok "missing accounts rc=0" || bad "missing accounts rc=$RC"
echo "$OUT" | grep -q "roblox_accounts.json" && ok "missing file named" || bad "missing not named"

# --- service handling ------------------------------------------------------------
mktree
mkarch "$T/s.tar.gz"
SM_NO_SVC=0 run_sm "$T/s.tar.gz"
[ "$RC" = 0 ] && ok "svc-path rc=0" || bad "svc-path rc=$RC"
grep -q "stop jerusalem" "$T/svc-calls" && grep -q "start jerusalem" "$T/svc-calls" \
    && ok "svc stop+start called" || bad "svc calls: $(cat "$T/svc-calls" 2>/dev/null)"

mktree
touch "$T/stop-fail"
mkarch "$T/sf.tar.gz"
SM_NO_SVC=0 run_sm "$T/sf.tar.gz"
[ "$RC" = 0 ] && ok "stop failure: import still proceeds" || bad "stop-fail rc=$RC"
echo "$OUT" | grep -q "stop jerusalem failed" && ok "stop failure warned" || bad "stop warn missing"

# --- idempotency ------------------------------------------------------------------
mktree
mkarch "$T/id.tar.gz"
run_sm "$T/id.tar.gz"; run_sm "$T/id.tar.gz"
[ "$RC" = 0 ] && ok "re-run rc=0" || bad "re-run rc=$RC"
[ "$(cat "$DATA/roblox_accounts.json")" = '{"src":"roblox_accounts.json"}' ] \
    && ok "re-run result stable" || bad "re-run changed result"

# --- script hygiene -----------------------------------------------------------------
bash -n "$SM" && ok "bash -n clean" || bad "bash -n failed"

echo "$PASS passed, $FAILC failed"
[ "$FAILC" = 0 ]
