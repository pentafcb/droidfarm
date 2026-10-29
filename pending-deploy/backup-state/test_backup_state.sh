#!/bin/bash
# test_backup_state.sh — hermetic tests for backup-state.sh + verify-backup.sh.
# Fake DATA/BACKUP/LOG dirs; no system paths touched. Final line format
# ("N passed, 0 failed") is what deploy-all.sh preflight greps.
set -u
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_SH="$SELF_DIR/backup-state.sh"
VERIFY_SH="$SELF_DIR/verify-backup.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $1"; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

# build a fake state dir with secret-looking content (for the no-leak tests)
seed_data() { # seed_data <dir>
    local d="$1"
    mkdir -p "$d/batch_jobs"
    printf '{"a":1}'            > "$d/instances.json"
    printf '{"b":2}'            > "$d/providers.json"
    printf '{"c":3}'            > "$d/tokens.json"
    printf '{"u":"x","p":"S3CR3T-pw-9f2k"}' > "$d/roblox_accounts.json"
    printf '{"r":4}'            > "$d/roblox_login_results.json"
    printf 'T0K3N-secret-abc'   > "$d/static_join_token"
    printf '{"job":"done"}'     > "$d/batch_jobs/a1b2c3d4e5f60718.json"
}
run_backup() { # run_backup <data> <bkdir>
    JERUSALEM_DATA="$1" JERUSALEM_BACKUPS="$2" \
    JERUSALEM_BACKUP_LOG="$T/backup.log" \
    bash "$BACKUP_SH" > "$T/out" 2>&1
    echo $?
}

echo "test: happy path archives the full state set, 600 perms, verifiable"
seed_data "$T/data"
rc="$(run_backup "$T/data" "$T/bk")"
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc (want 0)"
arc="$(ls "$T/bk"/jerusalem-state-*.tar.gz 2>/dev/null | head -1)"
[ -n "$arc" ] && ok "archive created: $(basename "$arc")" || bad "no archive"
[ -n "$arc" ] && [ "$(stat -c%a "$arc" 2>/dev/null)" = 600 ] && ok "perms 600" || bad "perms wrong"
if [ -n "$arc" ]; then
    members="$(tar -tzf "$arc" | sort | tr '\n' ' ')"
    exp="batch_jobs/a1b2c3d4e5f60718.json instances.json providers.json roblox_accounts.json roblox_login_results.json static_join_token tokens.json "
    [ "$members" = "$exp" ] && ok "members exactly the state set" || bad "members: $members"
    cmp -s "$T/data/roblox_accounts.json" <(tar -xzOf "$arc" roblox_accounts.json) \
        && ok "content round-trips byte-identical" || bad "content changed"
fi
grep -q "^.* ok files=7 " "$T/backup.log" && ok "log line written" || bad "no log line"

echo "test: missing state file warns but still archives the rest"
rm "$T/bk"/*.tar.gz
rm "$T/data/roblox_login_results.json"
rc="$(run_backup "$T/data" "$T/bk")"
[ "$rc" -eq 0 ] && ok "exit 0 with one file missing" || bad "exit $rc"
grep -q "WARNING.*roblox_login_results.json" "$T/out" && ok "missing file warned by name" || bad "no warning"
arc2="$(ls "$T/bk"/jerusalem-state-*.tar.gz 2>/dev/null | head -1)"
tar -tzf "$arc2" | grep -q roblox_login_results && bad "missing file archived anyway" || ok "missing file not in archive"

echo "test: zero state files -> fail closed, no archive, no prune"
rm -rf "$T/empty" "$T/bk2"; mkdir -p "$T/empty" "$T/bk2"
touch "$T/bk2/jerusalem-state-20000101-000000.tar.gz"   # old archive must survive
rc="$(run_backup "$T/empty" "$T/bk2")"
[ "$rc" -eq 1 ] && ok "exit 1" || bad "exit $rc (want 1)"
grep -q "FAIL" "$T/out" && ok "FAIL line printed" || bad "no FAIL line"
[ "$(ls "$T/bk2"/*.tar.gz | wc -l)" = 1 ] && ok "old archive not pruned on failure" || bad "prune ran on failure"

echo "test: corrupt JSON source -> fail closed, no final archive"
seed_data "$T/baddata"
printf '{oops' > "$T/baddata/instances.json"
rm -rf "$T/bk3"; mkdir -p "$T/bk3"
rc="$(run_backup "$T/baddata" "$T/bk3")"
[ "$rc" -eq 1 ] && ok "exit 1" || bad "exit $rc (want 1)"
grep -q "not valid JSON: instances.json" "$T/out" && ok "names the bad member" || bad "no bad-member line"
[ -z "$(ls "$T/bk3"/jerusalem-state-*.tar.gz 2>/dev/null)" ] && ok "no final archive left" || bad "bad archive kept"
[ -z "$(ls "$T/bk3"/.tmp-* 2>/dev/null)" ] && ok "tmp file cleaned" || bad "tmp file leaked"

echo "test: retention prunes only archives older than RETENTION_DAYS"
rm -rf "$T/bk4"; mkdir -p "$T/bk4"
touch -d '10 days ago' "$T/bk4/jerusalem-state-20000101-000000.tar.gz"
touch -d '3 days ago'  "$T/bk4/jerusalem-state-20000102-000000.tar.gz"
seed_data "$T/data4"
RETENTION_DAYS=7 rc2=0
JERUSALEM_DATA="$T/data4" JERUSALEM_BACKUPS="$T/bk4" JERUSALEM_BACKUP_LOG="$T/backup.log" \
  RETENTION_DAYS=7 bash "$BACKUP_SH" > "$T/out" 2>&1; rc2=$?
[ "$rc2" -eq 0 ] && ok "exit 0" || bad "exit $rc2"
[ ! -f "$T/bk4/jerusalem-state-20000101-000000.tar.gz" ] && ok "10-day archive pruned" || bad "old archive kept"
[ -f "$T/bk4/jerusalem-state-20000102-000000.tar.gz" ] && ok "3-day archive kept" || bad "young archive pruned"
[ "$(ls "$T/bk4"/jerusalem-state-*.tar.gz | wc -l)" = 2 ] && ok "new archive + young old = 2" || bad "wrong count"
grep -q "pruned=1" "$T/out" && ok "prune count logged" || bad "prune count missing"

echo "test: free-space pre-check"
seed_data "$T/data5"; rm -rf "$T/bk5"; mkdir -p "$T/bk5"
JERUSALEM_DATA="$T/data5" JERUSALEM_BACKUPS="$T/bk5" JERUSALEM_BACKUP_LOG="$T/backup.log" \
  MIN_FREE_MB=999999999 bash "$BACKUP_SH" > "$T/out" 2>&1; rc=$?
[ "$rc" -eq 1 ] && ok "exit 1 when disk too full" || bad "exit $rc"
[ -z "$(ls "$T/bk5"/*.tar.gz 2>/dev/null)" ] && ok "no archive written" || bad "archive written anyway"

echo "test: secrets never appear in stdout or the log"
seed_data "$T/data6"; rm -rf "$T/bk6"; mkdir -p "$T/bk6"
JERUSALEM_DATA="$T/data6" JERUSALEM_BACKUPS="$T/bk6" JERUSALEM_BACKUP_LOG="$T/backup.log" \
  bash "$BACKUP_SH" > "$T/out" 2>&1
! grep -q "S3CR3T-pw-9f2k\|T0K3N-secret-abc" "$T/out" "$T/backup.log" \
    && ok "no secret values in output or log" || bad "secret leaked"

echo "test: verify-backup.sh on a good archive"
rm -rf "$T/vgood" "$T/bkv"; mkdir -p "$T/vgood" "$T/bkv"
seed_data "$T/vgood"
run_backup "$T/vgood" "$T/bkv" >/dev/null
arc="$(ls "$T/bkv"/jerusalem-state-*.tar.gz 2>/dev/null | head -1)"
[ -n "$arc" ] && ok "fresh full archive built" || bad "no archive for verify test"
bash "$VERIFY_SH" "$arc" > "$T/vout" 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc"
grep -q "VERDICT: archive is restorable" "$T/vout" && ok "restorable verdict" || bad "no verdict"
! grep -q "S3CR3T-pw-9f2k\|T0K3N-secret-abc" "$T/vout" && ok "verify prints no secrets" || bad "verify leaked secrets"

echo "test: verify-backup.sh failure modes"
bash "$VERIFY_SH" > /dev/null 2>&1; [ $? -eq 2 ] && ok "no-arg -> exit 2" || bad "no-arg exit wrong"
bash "$VERIFY_SH" /nonexistent/x.tar.gz > /dev/null 2>&1; [ $? -eq 2 ] && ok "missing file -> exit 2" || bad "missing exit wrong"
printf 'garbage-not-a-tar' > "$T/garbage.tar.gz"
bash "$VERIFY_SH" "$T/garbage.tar.gz" > /dev/null 2>&1; [ $? -ne 0 ] && ok "garbage tar -> nonzero" || bad "garbage accepted"
# archive with corrupt JSON
rm -rf "$T/vbad" "$T/bkbad"; mkdir -p "$T/vbad" "$T/bkbad"
printf '{broken' > "$T/vbad/instances.json"
tar -czf "$T/bkbad/bad.tar.gz" -C "$T/vbad" instances.json
bash "$VERIFY_SH" "$T/bkbad/bad.tar.gz" > "$T/vout" 2>&1; rc=$?
[ "$rc" -eq 1 ] && ok "bad json -> exit 1" || bad "bad json exit $rc"
grep -q "PROBLEM: not valid JSON: instances.json" "$T/vout" && ok "names the bad member" || bad "no PROBLEM line"
# archive missing a core file
rm -rf "$T/vmiss"; mkdir -p "$T/vmiss"
printf '{"a":1}' > "$T/vmiss/tokens.json"
tar -czf "$T/bkbad/miss.tar.gz" -C "$T/vmiss" tokens.json
bash "$VERIFY_SH" "$T/bkbad/miss.tar.gz" > "$T/vout" 2>&1; rc=$?
[ "$rc" -eq 1 ] && ok "missing core file -> exit 1" || bad "missing-core exit $rc"
grep -q "PROBLEM: core state file missing: instances.json" "$T/vout" && ok "names the missing file" || bad "no missing-file line"
# archive without batch_jobs is still restorable (predates fleet jobs)
rm -rf "$T/vold"; mkdir -p "$T/vold"
for f in instances.json providers.json tokens.json roblox_accounts.json roblox_login_results.json; do printf '{"x":1}' > "$T/vold/$f"; done
printf 'tok' > "$T/vold/static_join_token"
tar -czf "$T/bkbad/old.tar.gz" -C "$T/vold" \
    instances.json providers.json tokens.json roblox_accounts.json roblox_login_results.json static_join_token
bash "$VERIFY_SH" "$T/bkbad/old.tar.gz" > "$T/vout" 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "no batch_jobs -> still exit 0" || bad "old-style archive rejected"
grep -qi "batch_jobs" "$T/vout" && ok "batch_jobs absence noted" || bad "no batch_jobs note"

echo "test: STATE_FILES stays in sync between the two scripts"
s1="$(grep '^STATE_FILES=' "$BACKUP_SH")"
s2="$(grep '^STATE_FILES=' "$VERIFY_SH")"
[ "$s1" = "$s2" ] && [ -n "$s1" ] && ok "identical STATE_FILES in both scripts" || bad "drift: $s1 != $s2"

echo "test: bash -n clean"
bash -n "$BACKUP_SH" && ok "backup-state.sh bash -n clean" || bad "backup-state.sh bash -n failed"
bash -n "$VERIFY_SH" && ok "verify-backup.sh bash -n clean" || bad "verify-backup.sh bash -n failed"

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
