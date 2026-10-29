#!/bin/bash
# test_post_deploy.sh — hermetic tests for pending-deploy/post-deploy/finish-deploy.sh
#
# Builds a fake VPS root: fake docker (one wd-rbx1 container), fake crontab,
# fake volumes/state dirs, and runs the REAL staged scripts through the
# orchestrator. Failure paths swap staged scripts for failing stubs.
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
FD="$REPO/pending-deploy/post-deploy/finish-deploy.sh"
PASS=0; FAILC=0
ok()  { PASS=$((PASS+1)); }
bad() { FAILC=$((FAILC+1)); echo "TEST-FAIL: $*"; }

# --- fixture: fake VPS root --------------------------------------------------
mktree() {
    T="$(mktemp -d)"
    BIN="$T/bin"; mkdir -p "$BIN"
    cat > "$BIN/docker" <<'EOF'
#!/bin/bash
if [ "${1:-}" = "ps" ]; then echo "wd-rbx1"; exit 0; fi
if [ "${1:-}" = "exec" ]; then
    c="$2"
    case " $* " in
        *android-optimize.sh*)
            lf="${VOLBASE:-}/$c-data/_data/data/media/0/jerusalem/optimize.log"
            { echo "[optimize] run at fake"; echo "[optimize] done"; } >> "$lf" ;;
    esac
    exit 0
fi
exit 0
EOF
    chmod +x "$BIN/docker"
    # fake crontab: state in a file; handles -l, -, and <file> (the repoint
    # script installs via `crontab <tmpfile>`, the switch via `crontab -`).
    sed "s|@STATE@|$T/crontab.txt|" > "$BIN/crontab" <<'EOF'
#!/bin/bash
ST="@STATE@"
if [ "${1:-}" = "-l" ]; then [ -f "$ST" ] && cat "$ST"; exit 0; fi
if [ "${1:-}" = "-" ]; then cat > "$ST"; exit 0; fi
if [ -f "${1:-}" ]; then cp "$1" "$ST"; exit 0; fi
echo "usage" >&2; exit 1
EOF
    chmod +x "$BIN/crontab"
    printf '#!/bin/bash\necho "uid=1000"\n' > "$BIN/id"; chmod +x "$BIN/id"
    mkdir -p "$T/cron.d"
    SD="$T/staged"; mkdir -p "$SD" "$SD/static" "$SD/wing"
    for f in h264_stream.py app.py; do
        echo "# stub $f" > "$SD/$f"
    done
    echo "# stub index" > "$SD/static/index.html"
    echo "# stub agent" > "$SD/wing/agent.py"
    echo "# stub install-sh" > "$SD/wing/install-wing.sh"
    printf '@echo off\r\n' > "$SD/wing/install-wing.bat"
    echo "# stub watchdog-dst" > "$T/watchdog.sh"
    cp "$REPO/pending-deploy/keepalive-v2/push-keepalive.sh" "$SD/"
    cp "$REPO/pending-deploy/booted-watcher/push-booted-watcher.sh" "$SD/"
    cp "$REPO/pending-deploy/adaptive-screencap/push-screencap.sh" "$SD/"
    cp "$REPO/pending-deploy/android-optimizer/push-optimizer.sh" "$SD/"
    cp "$REPO/pending-deploy/backup-cron-repoint/repoint-backup-cron.sh" "$SD/"
    cp "$REPO/pending-deploy/backup-state/backup-state.sh" "$SD/"
    cp "$REPO/pending-deploy/backup-state/verify-backup.sh" "$SD/"
    printf '#!/bin/bash\necho keepalive-src\n' > "$SD/roblox-keepalive.sh"
    printf '#!/bin/bash\necho watcher-src\n' > "$SD/booted-watcher.sh"
    printf '#!/bin/bash\necho loop-src\n' > "$SD/screencap-loop.sh"
    printf '#!/bin/sh\necho optimizer-src\n' > "$SD/android-optimize.sh"
    VB="$T/volumes"; mkdir -p "$VB/wd-rbx1-data/_data/data/media/0/jerusalem"
    touch "$VB/wd-rbx1-data/_data/data/media/0/jerusalem/booted"
    STA="$T/statedir"; mkdir -p "$STA/batch_jobs"
    echo '{"a":1}' > "$STA/instances.json"
    echo '{"b":2}' > "$STA/providers.json"
    echo '{"t":3}' > "$STA/tokens.json"
    echo '{"r":4}' > "$STA/roblox_accounts.json"
    echo '{"l":5}' > "$STA/roblox_login_results.json"
    echo "test-token-value" > "$STA/static_join_token"
    echo '{"job":"x"}' > "$STA/batch_jobs/abcdef0123456789.json"
    # default crontab: the legacy explicit-list backup line (pre-repoint form)
    printf '%s\n' '17 3 * * * root tar -czf /tmp/jerusalem-backup.tgz /root/droidfarm/instances.json /root/droidfarm/providers.json' > "$T/crontab.txt"
}

run_fd() { # -> exit code; output in $T/out.log
    FD_SKIP_ROOT_CHECK=1 FD_DF_DIR="$SD" FD_CRONTAB_BIN="$BIN/crontab" FD_CROND_DIR="$T/cron.d" \
    FD_WATCHDOG_DST="$T/watchdog.sh" \
    PUSH_SCREENCAP_SRC="$SD/screencap-loop.sh" PUSH_SCREENCAP_VOLBASE="$VB" VOLBASE="$VB" \
    JERUSALEM_DATA="$STA" JERUSALEM_BACKUPS="$T/backups" JERUSALEM_BACKUP_LOG="$T/backup.log" \
    RETENTION_DAYS=7 MIN_FREE_MB=1 BACKUP_CRONTAB="$BIN/crontab" \
    PATH="$BIN:/usr/bin:/bin" \
    bash "$FD" > "$T/out.log" 2>&1
}

stub() { # $1=staged-filename $2=exit-code [$3=marker-text]
    printf '#!/bin/bash\necho "STUB-RAN %s" >> "%s/calls"\nexit %s\n' "$1" "$T" "$2" > "$SD/$1"
    chmod +x "$SD/$1"
}

# --- 1. happy path: real scripts, fake docker/crontab -------------------------
mktree
run_fd; rc=$?
[ "$rc" = 0 ] && ok "happy path exit 0" || bad "happy path exit $rc"
for s in preflight keepalive booted-watcher screencap optimizer repoint cron-switch backup-drill; do
    grep -q "\[ok\]   $s" "$T/out.log" && ok "summary ok: $s" || bad "summary missing ok: $s"
done
grep -q "keepalive v2 running" "$T/out.log" && ok "push-keepalive ran" || bad "push-keepalive output missing"
grep -q "booted-watcher running" "$T/out.log" && ok "push-booted-watcher ran" || bad "push-booted-watcher output missing"
grep -q "adaptive loop running" "$T/out.log" && ok "push-screencap ran" || bad "push-screencap output missing"
grep -q "optimizer applied" "$T/out.log" && ok "push-optimizer ran" || bad "push-optimizer output missing"
grep -q "repoint-backup-cron" "$T/out.log" && ok "repoint ran" || bad "repoint output missing"
grep -q "backup-state.sh" "$T/crontab.txt" && ok "crontab switched to backup-state.sh" || bad "crontab not switched"
grep -q "droidfarm/instances.json" "$T/crontab.txt" && bad "legacy backup line survived" || ok "legacy backup line gone"
[ "$(grep -c "backup-state.sh" "$T/crontab.txt")" = 1 ] && ok "exactly one backup-state.sh cron line" || bad "cron line count wrong"
ls "$T"/backups/jerusalem-state-*.tar.gz >/dev/null 2>&1 && ok "archive produced" || bad "no archive"
grep -q "verify-backup" "$T/out.log" && ok "drill ran" || bad "drill output missing"
# step order: 1..7 in sequence
o1=$(grep -n "step 1:" "$T/out.log" | cut -d: -f1); o7=$(grep -n "step 7:" "$T/out.log" | cut -d: -f1)
[ -n "$o1" ] && [ -n "$o7" ] && [ "$o1" -lt "$o7" ] && ok "steps run in order" || bad "step order wrong"
grep -q "verify-live.sh --ssh" "$T/out.log" && ok "next-step pointer printed" || bad "next-step pointer missing"
# idempotency: second run
run_fd; rc=$?
[ "$rc" = 0 ] && ok "second run exit 0 (idempotent)" || bad "second run exit $rc"
[ "$(grep -c "backup-state.sh" "$T/crontab.txt")" = 1 ] && ok "rerun keeps single cron line" || bad "rerun duplicated cron line"

# --- 2. preflight: missing staged file ----------------------------------------
mktree
rm "$SD/wing/agent.py"
run_fd; rc=$?
[ "$rc" = 1 ] && ok "missing staged file -> exit 1" || bad "missing staged file exit $rc"
grep -q "agent.py" "$T/out.log" && ok "names the missing file" || bad "missing file not named"
grep -q "deploy-all.sh first" "$T/out.log" && ok "points at deploy-all.sh" || bad "no deploy-all pointer"
grep -q "step 1:" "$T/out.log" && bad "step ran despite failed preflight" || ok "no step ran after failed preflight"

# --- 3. preflight: docker missing ----------------------------------------------
mktree
FD_SKIP_ROOT_CHECK=1 FD_DF_DIR="$SD" FD_CRONTAB_BIN="$BIN/crontab" PATH="/usr/bin:/bin" \
    bash "$FD" > "$T/out.log" 2>&1; rc=$?
[ "$rc" = 1 ] && ok "docker missing -> exit 1" || bad "docker missing exit $rc"
grep -q "docker not found" "$T/out.log" && ok "docker-missing message" || bad "docker-missing message absent"

# --- 4. preflight: crontab binary missing --------------------------------------
mktree
FD_SKIP_ROOT_CHECK=1 FD_DF_DIR="$SD" FD_CRONTAB_BIN="/nonexistent/crontab" \
    PUSH_SCREENCAP_SRC="$SD/screencap-loop.sh" PUSH_SCREENCAP_VOLBASE="$VB" VOLBASE="$VB" \
    PATH="$BIN:/usr/bin:/bin" \
    bash "$FD" > "$T/out.log" 2>&1; rc=$?
[ "$rc" = 1 ] && ok "crontab missing -> exit 1" || bad "crontab missing exit $rc"
grep -q "17 3 \* \* \* root bash" "$T/out.log" && ok "manual cron recipe printed" || bad "manual recipe missing"

# --- 5. root check --------------------------------------------------------------
mktree
FD_DF_DIR="$SD" FD_CRONTAB_BIN="$BIN/crontab" PATH="$BIN:/usr/bin:/bin" \
    bash "$FD" > "$T/out.log" 2>&1; rc=$?
[ "$rc" = 1 ] && ok "non-root refused" || bad "non-root exit $rc"
grep -q "must run as root" "$T/out.log" && ok "root message" || bad "root message absent"

# --- 6. step failure aborts the run --------------------------------------------
mktree
stub push-keepalive.sh 1
run_fd; rc=$?
[ "$rc" = 1 ] && ok "failing step 1 -> exit 1" || bad "failing step 1 exit $rc"
grep -q "step 1 FAILED" "$T/out.log" && ok "step 1 FAILED reported" || bad "step 1 FAILED not reported"
grep -q "\[FAIL\] keepalive" "$T/out.log" && ok "summary FAIL keepalive" || bad "summary FAIL keepalive missing"
grep -q "backup-state.sh" "$T/crontab.txt" && bad "later step ran after failure" || ok "no later step ran after failure"
grep -q "step 2:" "$T/out.log" && bad "step 2 ran after step 1 failed" || ok "stopped after step 1"

# --- 6b. optimizer step failure aborts the run ------------------------------------
mktree
stub push-optimizer.sh 1
run_fd; rc=$?
[ "$rc" = 1 ] && ok "failing optimizer step -> exit 1" || bad "failing optimizer step exit $rc"
grep -q "step 4 FAILED" "$T/out.log" && ok "step 4 FAILED reported" || bad "step 4 FAILED not reported"
grep -q "\[FAIL\] optimizer" "$T/out.log" && ok "summary FAIL optimizer" || bad "summary FAIL optimizer missing"
grep -q "backup-state.sh" "$T/crontab.txt" && bad "later step ran after optimizer failure" || ok "no later step ran after optimizer failure"
grep -q "step 5:" "$T/out.log" && bad "step 5 ran after optimizer failed" || ok "stopped after optimizer step"

# --- 6c. preflight catches a missing staged push script -------------------------
mktree
rm "$SD/push-keepalive.sh"
run_fd; rc=$?
[ "$rc" = 1 ] && ok "missing push script -> exit 1" || bad "missing push script exit $rc"
grep -q "push-keepalive.sh" "$T/out.log" && ok "names the missing push script" || bad "missing push script not named"
grep -q "step 1:" "$T/out.log" && bad "step ran despite missing push script" || ok "no step ran after failed preflight"

# --- 7. cron already switched -> no-op ------------------------------------------
mktree
printf '%s\n' "17 3 * * * root bash $SD/backup-state.sh" > "$T/crontab.txt"
cp "$T/crontab.txt" "$T/crontab.before"
run_fd; rc=$?
[ "$rc" = 0 ] && ok "already-switched exit 0" || bad "already-switched exit $rc"
cmp -s "$T/crontab.before" "$T/crontab.txt" && ok "already-switched crontab untouched" || bad "already-switched crontab changed"
grep -q "already calls backup-state.sh" "$T/out.log" && ok "already-switched noted" || bad "already-switched note missing"

# --- 8. no backup cron at all -> fail closed, never fabricate -------------------
mktree
printf '%s\n' '0 * * * * root /usr/bin/true' > "$T/crontab.txt"
cp "$T/crontab.txt" "$T/crontab.before"
run_fd; rc=$?
[ "$rc" = 1 ] && ok "no backup cron -> exit 1" || bad "no backup cron exit $rc"
grep -q "refusing to fabricate" "$T/out.log" && ok "no-fabrication message" || bad "no-fabrication message missing"
cmp -s "$T/crontab.before" "$T/crontab.txt" && ok "crontab untouched on fail-closed" || bad "crontab changed on fail-closed"

# --- 9. backup-state.sh failure -> abort, verify not run -------------------------
mktree
stub backup-state.sh 1
stub verify-backup.sh 0
run_fd; rc=$?
[ "$rc" = 1 ] && ok "backup failure -> exit 1" || bad "backup failure exit $rc"
grep -q "backup-state.sh run failed" "$T/out.log" && ok "backup failure reported" || bad "backup failure not reported"
grep -q "STUB-RAN verify-backup.sh" "$T/calls" 2>/dev/null && bad "verify ran after backup failed" || ok "verify not run after backup failed"

# --- 10. verify-backup.sh failure -> abort ---------------------------------------
mktree
stub verify-backup.sh 1
run_fd; rc=$?
[ "$rc" = 1 ] && ok "drill failure -> exit 1" || bad "drill failure exit $rc"
grep -q "verify-backup.sh FAILED" "$T/out.log" && ok "drill failure reported" || bad "drill failure not reported"

# --- 11. cron $(...) is never executed (heredoc-expansion regression) -------------
mktree
printf '%s\n' '17 3 * * * root tar -czf /tmp/backup-$(touch "$T/PWNED").tgz /root/droidfarm/instances.json' > "$T/crontab.txt"
run_fd; rc=$?
[ "$rc" = 0 ] && ok "\$(...) line handled, exit 0" || bad "\$(...) line exit $rc"
[ -e "$T/PWNED" ] && bad "cron \$(...) was EXECUTED" || ok "cron \$(...) never executed"
grep -q "backup-state.sh" "$T/crontab.txt" && ok "cmdsubst line replaced" || bad "cmdsubst line not replaced"

# --- 12. canonical backup cron in /etc/cron.d, personal clean -> no-op ------
# (the new-box case: baseline-newbox.sh installs /etc/cron.d/jerusalem-backup;
# the old personal-crontab-only step 6 aborted the whole run here)
mktree
printf '%s\n' '0 * * * * root /usr/bin/true' > "$T/crontab.txt"
printf '%s\n' "17 3 * * * root bash $SD/backup-state.sh" > "$T/cron.d/jerusalem-backup"
cp "$T/crontab.txt" "$T/crontab.before"; cp "$T/cron.d/jerusalem-backup" "$T/crond.before"
run_fd; rc=$?
[ "$rc" = 0 ] && ok "cron.d canonical -> exit 0" || bad "cron.d canonical exit $rc"
grep -q "already calls backup-state.sh" "$T/out.log" && ok "cron.d canonical noted" || bad "cron.d canonical note missing"
cmp -s "$T/crontab.before" "$T/crontab.txt" && ok "personal crontab untouched" || bad "personal crontab changed"
cmp -s "$T/crond.before" "$T/cron.d/jerusalem-backup" && ok "cron.d file untouched" || bad "cron.d file changed"

# --- 13. legacy backup line in /etc/cron.d only -> replaced in place --------
mktree
printf '%s\n' '0 * * * * root /usr/bin/true' > "$T/crontab.txt"
printf '%s\n' '17 3 * * * root tar -czf /tmp/jerusalem-backup.tgz /root/droidfarm/instances.json' '0 * * * * root /usr/bin/true' > "$T/cron.d/old-backup"
cp "$T/crontab.txt" "$T/crontab.before"
run_fd; rc=$?
[ "$rc" = 0 ] && ok "cron.d legacy -> exit 0" || bad "cron.d legacy exit $rc"
grep -q "droidfarm/instances.json" "$T/cron.d/old-backup" && bad "cron.d legacy line survived" || ok "cron.d legacy line gone"
grep -qx "17 3 \* \* \* root bash $SD/backup-state.sh" "$T/cron.d/old-backup" && ok "cron.d canonical line (user field kept)" || bad "cron.d canonical line wrong"
grep -qx "0 \* \* \* \* root /usr/bin/true" "$T/cron.d/old-backup" && ok "cron.d other lines preserved" || bad "cron.d other lines damaged"
cmp -s "$T/crontab.before" "$T/crontab.txt" && ok "personal crontab untouched" || bad "personal crontab changed"

# --- 14. personal-crontab replacement carries no user field -------------------
# (a 6-field line in a personal crontab would try to run "root" as a command
# and silently never back up)
mktree
run_fd; rc=$?
[ "$rc" = 0 ] && ok "personal switch exit 0" || bad "personal switch exit $rc"
grep -qx "17 3 \* \* \* bash $SD/backup-state.sh" "$T/crontab.txt" && ok "personal replacement is 5-field" || bad "personal replacement not 5-field"
grep -q "root bash $SD/backup-state.sh" "$T/crontab.txt" && bad "user field leaked into personal crontab" || ok "no user field in personal crontab"

# --- 15. legacy lines in both locations -> both switched + warning -----------
mktree
printf '%s\n' '17 3 * * * root tar -czf /tmp/jerusalem-backup-old.tgz /root/droidfarm/x' > "$T/cron.d/old-backup"
run_fd; rc=$?
[ "$rc" = 0 ] && ok "both locations -> exit 0" || bad "both locations exit $rc"
grep -qx "17 3 \* \* \* bash $SD/backup-state.sh" "$T/crontab.txt" && ok "personal switched (5-field)" || bad "personal not switched"
grep -qx "17 3 \* \* \* root bash $SD/backup-state.sh" "$T/cron.d/old-backup" && ok "cron.d switched (6-field)" || bad "cron.d not switched"
grep -q "2 locations" "$T/out.log" && ok "multi-location warning logged" || bad "multi-location warning missing"

# --- 16. canonical in personal + legacy in cron.d -> cron.d switched only ----
mktree
printf '%s\n' "17 3 * * * root bash $SD/backup-state.sh" > "$T/crontab.txt"
printf '%s\n' '17 3 * * * root tar -czf /tmp/jerusalem-backup-old.tgz /root/droidfarm/x' > "$T/cron.d/old-backup"
cp "$T/crontab.txt" "$T/crontab.before"
run_fd; rc=$?
[ "$rc" = 0 ] && ok "mixed canonical/legacy -> exit 0" || bad "mixed exit $rc"
cmp -s "$T/crontab.before" "$T/crontab.txt" && ok "personal canonical preserved byte-identical" || bad "personal canonical rewritten"
grep -qx "17 3 \* \* \* root bash $SD/backup-state.sh" "$T/cron.d/old-backup" && ok "cron.d legacy switched" || bad "cron.d legacy not switched"

# --- 17. unreadable cron.d file -> fail closed --------------------------------
mktree
printf '%s\n' '0 * * * * root /usr/bin/true' > "$T/crontab.txt"
printf '%s\n' '17 3 * * * root tar -czf /tmp/old.tgz /root/droidfarm/x # backup' > "$T/cron.d/locked"
chmod 000 "$T/cron.d/locked"
if [ "$(id -u)" = 0 ]; then
    echo "  skip (running as root: 000 file still readable)"
    PASS=$((PASS+1))
else
    run_fd; rc=$?
    [ "$rc" = 1 ] && ok "unreadable cron.d -> exit 1" || bad "unreadable cron.d exit $rc"
fi
chmod 644 "$T/cron.d/locked"

# --- 18. no backup anywhere (cron.d present, non-backup file) -> fail closed -
mktree
printf '%s\n' '0 * * * * root /usr/bin/true' > "$T/crontab.txt"
printf '%s\n' '0 * * * * root /usr/bin/true' > "$T/cron.d/other"
cp "$T/crontab.txt" "$T/crontab.before"; cp "$T/cron.d/other" "$T/crond.before"
run_fd; rc=$?
[ "$rc" = 1 ] && ok "no backup anywhere -> exit 1" || bad "no backup anywhere exit $rc"
grep -q "refusing to fabricate" "$T/out.log" && ok "no-fabrication message" || bad "no-fabrication message missing"
cmp -s "$T/crontab.before" "$T/crontab.txt" && ok "personal untouched" || bad "personal changed"
cmp -s "$T/crond.before" "$T/cron.d/other" && ok "cron.d file untouched" || bad "cron.d file changed"

echo "$PASS passed, $FAILC failed"
[ "$FAILC" = 0 ]
