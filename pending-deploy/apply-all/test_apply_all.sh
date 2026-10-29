#!/bin/bash
# test_apply_all.sh — tests for pending-deploy/apply-all (vps-apply.sh + deploy-all.sh).
# Builds a fake VPS root in /tmp plus shim systemctl/curl/journalctl/apt-get,
# then runs vps-apply.sh against it with failure injection.
set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
APPLY="$REPO/pending-deploy/apply-all/vps-apply.sh"
DEPLOY_ALL="$REPO/pending-deploy/apply-all/deploy-all.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL $1"; }

# ---------- fake VPS builder ----------
# setup_fake creates $T with old deployed files, stage with new files, shims.
setup_fake() {
    T="$(mktemp -d /tmp/fakevps-XXXXXX)"
    export FAKE_ROOT="$T"
    mkdir -p "$T/bin" "$T/root/jerusalem/wing" "$T/stage" "$T/usr/local/bin"
    printf 'OLD-app'       > "$T/root/jerusalem/app.py"
    printf 'OLD-agent'      > "$T/root/jerusalem/wing/agent.py"
    printf 'OLD-watchdog'   > "$T/usr/local/bin/df-watchdog.sh"
    printf 'print("new-app")'         > "$T/stage/app.py"
    printf 'print("new-h264")'        > "$T/stage/h264_stream.py"
    printf '<html>new-index</html>'   > "$T/stage/index.html"
    printf 'print("new-agent")'       > "$T/stage/agent.py"
    printf '#!/bin/sh\necho new-watchdog\n' > "$T/stage/df-watchdog.sh"
    printf '#!/bin/sh\necho new-keepalive\n' > "$T/stage/roblox-keepalive.sh"
    printf '#!/bin/sh\necho new-bootedwatcher\n' > "$T/stage/booted-watcher.sh"
    printf '#!/bin/sh\necho new-repoint\n' > "$T/stage/repoint-backup-cron.sh"
    printf '#!/bin/sh\necho new-screencap\n' > "$T/stage/screencap-loop.sh"
    printf '#!/bin/sh\necho new-pushscreencap\n' > "$T/stage/push-screencap.sh"
    printf '#!/bin/sh\necho new-backupstate\n' > "$T/stage/backup-state.sh"
    printf '#!/bin/sh\necho new-verifybackup\n' > "$T/stage/verify-backup.sh"
    printf '#!/bin/sh\necho new-finishdeploy\n' > "$T/stage/finish-deploy.sh"
    printf '#!/bin/sh\necho new-pushkeepalive\n' > "$T/stage/push-keepalive.sh"
    printf '#!/bin/sh\necho new-pushbootedwatcher\n' > "$T/stage/push-booted-watcher.sh"
    printf '#!/bin/sh\necho new-androadopt\n' > "$T/stage/android-optimize.sh"
    printf '#!/bin/sh\necho new-pushoptimizer\n' > "$T/stage/push-optimizer.sh"
    printf '#!/bin/sh\necho new-installsh\n' > "$T/stage/install-wing.sh"
    printf '@echo off\r\necho new-installbat\r\n' > "$T/stage/install-wing.bat"

    cat > "$T/bin/systemctl" <<'EOF'
#!/bin/bash
: "${FAKE_ROOT:?shim needs FAKE_ROOT}"
echo "$*" >> "$FAKE_ROOT/systemctl.log"
[ "${SYS_FAIL:-0}" = 1 ] && exit 1
if [ "$1" = "is-active" ]; then
    # the vps-apply preflight probes the LEGACY pre-rename unit first:
    # a live droidfarm unit must report inactive or the deploy aborts
    case "$*" in *droidfarm*) [ "${LEGACY_ACTIVE:-0}" = 1 ] && { echo "active"; exit 0; }; exit 1;; esac
    n=$(cat "$FAKE_ROOT/isactive_count" 2>/dev/null || echo 0); n=$((n+1))
    echo "$n" > "$FAKE_ROOT/isactive_count"
    [ "$n" -le "${ISACTIVE_FAIL_FIRST:-0}" ] && exit 1
    echo "active"; exit 0
fi
if [ "$1" = "restart" ]; then
    n=$(cat "$FAKE_ROOT/restart_count" 2>/dev/null || echo 0); n=$((n+1))
    echo "$n" > "$FAKE_ROOT/restart_count"
    { [ "${SYS_FAIL_RESTART:-0}" = 1 ] || [ "$n" -ge "${RESTART_FAIL_FROM_N:-999}" ]; } && exit 1
    exit 0
fi
exit 0
EOF
    cat > "$T/bin/curl" <<'EOF'
#!/bin/bash
: "${FAKE_ROOT:?shim needs FAKE_ROOT}"
url="${@: -1}"
echo "$url" >> "$FAKE_ROOT/curl.log"
case "$url" in
    */ws/h264/*) printf '%s' "${CURL_H264_CODE:-401}" ;;
    *)           printf '200' ;;
esac
EOF
    cat > "$T/bin/journalctl" <<'EOF'
#!/bin/bash
: "${FAKE_ROOT:?shim needs FAKE_ROOT}"
echo "DroidFarm manager on :8080 (PIL: True, WebRTC: True, H.264: ${BANNER_H264:-True})"
EOF
    cat > "$T/bin/apt-get" <<'EOF'
#!/bin/bash
: "${FAKE_ROOT:?shim needs FAKE_ROOT}"
echo "$*" >> "$FAKE_ROOT/apt.log"
[ "${APT_FAIL:-0}" = 1 ] && exit 1
printf '#!/bin/sh\nexit 0\n' > "$FAKE_ROOT/bin/ffmpeg"; chmod +x "$FAKE_ROOT/bin/ffmpeg"
exit 0
EOF
    chmod +x "$T/bin/"*
    [ "${WHICH_FFMPEG:-1}" = 1 ] && { printf '#!/bin/sh\nexit 0\n' > "$T/bin/ffmpeg"; chmod +x "$T/bin/ffmpeg"; }
}

run_apply() {
    # extra env assignments may precede via RUN_ENV
    env -i PATH="$T/bin:/usr/bin:/bin" HOME="$HOME" FAKE_ROOT="$T" ${RUN_ENV:-} \
        DF_DIR="$T/root/jerusalem" STAGE_DIR="$T/stage" BACKUP_BASE="$T/backups" \
        WATCHDOG_DST="$T/usr/local/bin/df-watchdog.sh" WATCHDOG_LOG="$T/watchdog.log" \
        DEGRADED_DIR="$T/degraded" SKIP_WATCHDOG_WAIT=1 KEEP_BACKUPS=5 \
        DEPLOY_GIT_HASH=test123 \
        bash "$APPLY" >"$T/out.log" 2>"$T/err.log"
}

teardown() { rm -rf "$T"; unset FAKE_ROOT RUN_ENV; }

# ---------- tests ----------
t_happy_path() {
    echo "test: happy path deploys all 12 files, backs up, restarts, verifies"
    setup_fake
    # seed old backups to test pruning (7 old + 1 new = keep 5)
    for i in 1 2 3 4 5 6 7; do mkdir -p "$T/backups/2020010$i-000000"; done
    run_apply; rc=$?
    [ "$rc" = 0 ] || bad "exit=$rc (want 0)"
    [ "$rc" = 0 ] && ok "exit 0"
    grep -q "new-app" "$T/root/jerusalem/app.py" && ok "app.py deployed" || bad "app.py not deployed"
    grep -q "new-h264" "$T/root/jerusalem/h264_stream.py" && ok "h264_stream.py deployed" || bad "h264_stream.py missing"
    grep -q "new-index" "$T/root/jerusalem/static/index.html" && ok "index.html deployed (static/ created)" || bad "index.html missing"
    grep -q "new-agent" "$T/root/jerusalem/wing/agent.py" && ok "agent.py deployed" || bad "agent.py missing"
    grep -q "new-watchdog" "$T/usr/local/bin/df-watchdog.sh" && ok "watchdog deployed" || bad "watchdog missing"
    grep -q "new-keepalive" "$T/root/jerusalem/roblox-keepalive.sh" && ok "keepalive v2 deployed" || bad "keepalive missing"
    grep -q "new-bootedwatcher" "$T/root/jerusalem/booted-watcher.sh" && ok "booted-watcher deployed" || bad "booted-watcher missing"
    grep -q "new-repoint" "$T/root/jerusalem/repoint-backup-cron.sh" && ok "repoint-backup-cron.sh deployed" || bad "repoint-backup-cron.sh missing"
    grep -q "new-screencap" "$T/root/jerusalem/screencap-loop.sh" && ok "screencap-loop.sh deployed" || bad "screencap-loop.sh missing"
    grep -q "new-pushscreencap" "$T/root/jerusalem/push-screencap.sh" && ok "push-screencap.sh deployed" || bad "push-screencap.sh missing"
    grep -q "new-backupstate" "$T/root/jerusalem/backup-state.sh" && ok "backup-state.sh deployed" || bad "backup-state.sh missing"
    grep -q "new-verifybackup" "$T/root/jerusalem/verify-backup.sh" && ok "verify-backup.sh deployed" || bad "verify-backup.sh missing"
    grep -q "new-finishdeploy" "$T/root/jerusalem/finish-deploy.sh" && ok "finish-deploy.sh deployed" || bad "finish-deploy.sh missing"
    grep -q "new-pushkeepalive" "$T/root/jerusalem/push-keepalive.sh" && ok "push-keepalive.sh deployed" || bad "push-keepalive.sh missing"
    grep -q "new-pushbootedwatcher" "$T/root/jerusalem/push-booted-watcher.sh" && ok "push-booted-watcher.sh deployed" || bad "push-booted-watcher.sh missing"
    grep -q "new-androadopt" "$T/root/jerusalem/android-optimize.sh" && ok "android-optimize.sh deployed" || bad "android-optimize.sh missing"
    grep -q "new-pushoptimizer" "$T/root/jerusalem/push-optimizer.sh" && ok "push-optimizer.sh deployed" || bad "push-optimizer.sh missing"
    grep -q "new-installsh" "$T/root/jerusalem/wing/install-wing.sh" && ok "install-wing.sh deployed to wing/" || bad "install-wing.sh missing"
    grep -q "new-installbat" "$T/root/jerusalem/wing/install-wing.bat" && ok "install-wing.bat deployed to wing/" || bad "install-wing.bat missing"
    nb="$(ls -1d "$T/backups"/*/ 2>/dev/null | wc -l)"
    [ "$nb" = 5 ] && ok "backups pruned to 5" || bad "backup count=$nb (want 5)"
    latest="$(ls -1dt "$T/backups"/*/ | head -1)"
    grep -q "OLD-app" "$latest/app.py" && ok "backup holds old app.py" || bad "backup content wrong"
    [ -f "$latest/MANIFEST" ] && ok "MANIFEST written" || bad "MANIFEST missing"
    grep -q "restart jerusalem" "$T/systemctl.log" && ok "service restarted" || bad "no restart logged"
    grep -q "DONE" "$T/out.log" && ok "DONE summary" || bad "no DONE summary"
    teardown
}

t_syntax_failure() {
    echo "test: broken staged app.py -> exit 2, files restored, NO restart"
    setup_fake
    printf 'def broken(:\n' > "$T/stage/app.py"
    run_apply; rc=$?
    [ "$rc" = 2 ] || bad "exit=$rc (want 2)"
    [ "$rc" = 2 ] && ok "exit 2"
    grep -q "OLD-app" "$T/root/jerusalem/app.py" && ok "old app.py restored" || bad "app.py not restored"
    grep -q "restart" "$T/systemctl.log" 2>/dev/null && bad "restart happened (should not)" || ok "no restart attempted"
    teardown
}

t_restart_unhealthy_rollback() {
    echo "test: is-active fails after restart -> exit 2, old code back, restarted twice"
    setup_fake
    RUN_ENV="ISACTIVE_FAIL_FIRST=1" run_apply; rc=$?
    [ "$rc" = 2 ] || bad "exit=$rc (want 2)"
    [ "$rc" = 2 ] && ok "exit 2"
    grep -q "OLD-app" "$T/root/jerusalem/app.py" && ok "old app.py restored" || bad "not restored"
    n="$(grep -c "^restart jerusalem" "$T/systemctl.log")"
    [ "$n" = 2 ] && ok "restarted twice (deploy + rollback)" || bad "restart count=$n (want 2)"
    teardown
}

t_rollback_verify_failure() {
    echo "test: deploy restart ok, then everything fails -> exit 3 (manual intervention)"
    setup_fake
    RUN_ENV="ISACTIVE_FAIL_FIRST=99 RESTART_FAIL_FROM_N=2" run_apply; rc=$?
    [ "$rc" = 3 ] || bad "exit=$rc (want 3)"
    [ "$rc" = 3 ] && ok "exit 3"
    grep -q "MANUAL INTERVENTION" "$T/err.log" && ok "manual-intervention flagged" || bad "not flagged"
    teardown
}

t_h264_501_warning() {
    echo "test: h264 probe 501 + no ffmpeg -> exit 0 with warning, files stay deployed"
    setup_fake
    RUN_ENV="CURL_H264_CODE=501 BANNER_H264=False WHICH_FFMPEG=0 APT_FAIL=1" run_apply; rc=$?
    [ "$rc" = 0 ] || bad "exit=$rc (want 0)"
    [ "$rc" = 0 ] && ok "exit 0"
    grep -q "WARNING" "$T/err.log" && ok "warning emitted" || bad "no warning"
    grep -q "new-app" "$T/root/jerusalem/app.py" && ok "deploy kept (no rollback on warning)" || bad "rolled back on warning"
    teardown
}

t_h264_404_rolls_back() {
    echo "test: h264 probe 404 -> exit 2, rolled back"
    setup_fake
    RUN_ENV="CURL_H264_CODE=404" run_apply; rc=$?
    [ "$rc" = 2 ] || bad "exit=$rc (want 2)"
    [ "$rc" = 2 ] && ok "exit 2"
    grep -q "OLD-app" "$T/root/jerusalem/app.py" && ok "old code restored" || bad "not restored"
    teardown
}

t_extra_stage_file() {
    echo "test: unexpected file in stage -> exit 1, nothing touched"
    setup_fake
    printf 'evil' > "$T/stage/evil.sh"
    run_apply; rc=$?
    [ "$rc" = 1 ] || bad "exit=$rc (want 1)"
    [ "$rc" = 1 ] && ok "exit 1"
    [ ! -d "$T/backups" ] && ok "no backup dir (nothing touched)" || bad "backup dir created"
    grep -q "OLD-app" "$T/root/jerusalem/app.py" && ok "targets untouched" || bad "targets modified"
    teardown
}

t_missing_stage_file() {
    echo "test: missing stage file -> exit 1, nothing touched"
    setup_fake
    rm "$T/stage/agent.py"
    run_apply; rc=$?
    [ "$rc" = 1 ] || bad "exit=$rc (want 1)"
    [ "$rc" = 1 ] && ok "exit 1"
    [ ! -d "$T/backups" ] && ok "nothing touched" || bad "backup dir created"
    teardown
}

t_state_file_guard() {
    echo "test: state-like filename in stage -> refused (exit 1)"
    setup_fake
    printf '{}' > "$T/stage/instances.json"
    run_apply; rc=$?
    [ "$rc" = 1 ] || bad "exit=$rc (want 1)"
    [ "$rc" = 1 ] && ok "exit 1"
    teardown
}

t_legacy_unit_guard() {
    echo "test: legacy droidfarm.service ACTIVE -> preflight aborts (exit 1), nothing touched"
    setup_fake
    RUN_ENV="LEGACY_ACTIVE=1" run_apply; rc=$?
    [ "$rc" = 1 ] || bad "exit=$rc (want 1)"
    [ "$rc" = 1 ] && ok "exit 1"
    grep -q "legacy droidfarm.service is ACTIVE" "$T/err.log" && ok "clear remediation message" \
        || bad "no legacy-unit message in stderr"
    grep -q "OLD-app" "$T/root/jerusalem/app.py" && ok "targets untouched" || bad "targets modified"
    teardown
}

t_new_file_rollback_deletes() {
    echo "test: rollback deletes newly-created target (h264_stream.py did not exist)"
    setup_fake
    # h264_stream.py target never existed -> backup skips it -> rollback must remove it
    printf 'def broken(:\n' > "$T/stage/app.py"   # force failure after install
    run_apply; rc=$?
    [ "$rc" = 2 ] || bad "exit=$rc (want 2)"
    [ ! -f "$T/root/jerusalem/h264_stream.py" ] && ok "newly-created file removed on rollback" \
        || bad "newly-created file left behind"
    grep -q "OLD-app" "$T/root/jerusalem/app.py" && ok "existing file restored" || bad "existing file not restored"
    teardown
}

t_deploy_all_stage_only() {
    echo "test: deploy-all.sh --stage-only builds a correct stage dir (no ssh)"
    out="$(bash "$DEPLOY_ALL" --stage-only --skip-preflight --yes 2>&1)"
    rc=$?
    [ "$rc" = 0 ] || { bad "exit=$rc: $out"; return; }
    ok "exit 0"
    stagedir="$(echo "$out" | grep -o '/tmp/jerusalem-deploy-[0-9-]*' | head -1)"
    [ -n "$stagedir" ] || { bad "stage dir not reported"; return; }
    for f in app.py h264_stream.py index.html agent.py df-watchdog.sh roblox-keepalive.sh booted-watcher.sh repoint-backup-cron.sh screencap-loop.sh push-screencap.sh backup-state.sh verify-backup.sh finish-deploy.sh push-keepalive.sh push-booted-watcher.sh android-optimize.sh push-optimizer.sh install-wing.sh install-wing.bat vps-apply.sh MANIFEST; do
        [ -f "$stagedir/$f" ] || [ -f "$stagedir/stage/$f" ] || { bad "missing $f in stage"; return; }
    done
    ok "stage contains 19 files + vps-apply.sh + MANIFEST"
    diff -q "$stagedir/stage/app.py" "$REPO/manager/app.py" >/dev/null && ok "staged app.py == repo HEAD" || bad "staged app.py differs"
    diff -q "$stagedir/stage/roblox-keepalive.sh" "$REPO/waydroid-image/roblox-keepalive.sh" >/dev/null && ok "staged keepalive == repo HEAD" || bad "staged keepalive differs"
    rm -rf "$stagedir"
}

# ---------- deploy-all.sh transport tests (fake scp/ssh, real nesting semantics) ----------
# write_fake_transport: $T/rbin/{scp,ssh} used via DF_SCP/DF_SSH/SSH_OPTS="".
# The fake scp implements REAL `scp -r` semantics (target dir exists -> nest
# the source inside it; target missing -> rename onto it) against the fake
# remote root $T (remote /root/... maps to $T/root/...). The fake ssh
# emulates `mkdir -p <path>` and executes `bash <remote vps-apply.sh>` with
# the same env the other tests use for vps-apply.sh (so STAGE_DIR resolves
# from the script's real SELF_DIR, exactly like on the VPS).
write_fake_transport() {
    mkdir -p "$T/rbin"
    cat > "$T/rbin/scp" <<'SCPEOF'
#!/bin/bash
: "${FAKE_ROOT:?fake scp needs FAKE_ROOT}"
echo "$@" >> "$FAKE_ROOT/scp.log"
pos=(); skip=0
for a in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$a" in -o) skip=1 ;; -*) continue ;; *) pos+=("$a") ;; esac
done
n=${#pos[@]}
src="${pos[$((n-2))]}"
rdst="$(printf '%s' "${pos[$((n-1))]}" | sed 's/^[^:]*://; s|/$||')"
mdst="$(printf '%s' "$rdst" | sed "s|^/root/|$FAKE_ROOT/root/|")"
if [ -e "$mdst" ]; then cp -r "$src" "$mdst/"; else cp -r "$src" "$mdst"; fi
SCPEOF
    cat > "$T/rbin/ssh" <<'SSHEOF'
#!/bin/bash
: "${FAKE_ROOT:?fake ssh needs FAKE_ROOT}"
echo "$@" >> "$FAKE_ROOT/ssh.log"
pos=(); skip=0
for a in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$a" in -o) skip=1 ;; -*) continue ;; *) pos+=("$a") ;; esac
done
cmd="$(printf '%s ' "${pos[@]:1}")"
map() { printf '%s' "$1" | sed "s|^/root/|$FAKE_ROOT/root/|"; }
case "$cmd" in
    *mkdir\ -p*)
        p="$(printf '%s' "$cmd" | sed "s/.*mkdir -p ['\"]//; s/['\"].*//")"
        mkdir -p "$(map "$p")" ;;
    *bash\ /root/*)
        script="$(printf '%s' "$cmd" | sed 's/.*bash \([^ ]*\).*/\1/')"
        gh="$(printf '%s' "$cmd" | sed 's/.*DEPLOY_GIT_HASH=\([^ ]*\).*/\1/; t; s/.*/unknown/')"
        env -i PATH="$FAKE_ROOT/bin:/usr/bin:/bin" HOME="$HOME" FAKE_ROOT="$FAKE_ROOT" \
            DF_DIR="$FAKE_ROOT/root/jerusalem" BACKUP_BASE="$FAKE_ROOT/backups" \
            WATCHDOG_DST="$FAKE_ROOT/usr/local/bin/df-watchdog.sh" \
            WATCHDOG_LOG="$FAKE_ROOT/watchdog.log" DEGRADED_DIR="$FAKE_ROOT/degraded" \
            SKIP_WATCHDOG_WAIT=1 KEEP_BACKUPS=5 DEPLOY_GIT_HASH="$gh" \
            bash "$(map "$script")" ;;
    *) echo "fake ssh: unsupported command: $cmd" >&2; exit 99 ;;
esac
SSHEOF
    chmod +x "$T/rbin/"*
}

t_deploy_all_rerun_fresh_stage() {
    echo "test: deploy-all.sh re-run executes the NEW stage, not the first run's"
    setup_fake
    write_fake_transport
    # seed 4 previous run dirs; the oldest carries a STALE stub vps-apply.sh
    # that marks itself if it is ever executed
    for ts in 20000101-000000 20000102-000000 20000103-000000 20000104-000000; do
        mkdir -p "$T/root/jerusalem-deploy/jerusalem-deploy-$ts/stage"
    done
    cat > "$T/root/jerusalem-deploy/jerusalem-deploy-20000101-000000/vps-apply.sh" <<EOF
#!/bin/bash
echo STALE > "$T/stale-executed"
exit 0
EOF
    chmod +x "$T/root/jerusalem-deploy/jerusalem-deploy-20000101-000000/vps-apply.sh"
    out="$(FAKE_ROOT="$T" DF_SCP="$T/rbin/scp" DF_SSH="$T/rbin/ssh" SSH_OPTS="" \
        bash "$DEPLOY_ALL" fakehost --yes --skip-preflight 2>&1)"
    rc=$?
    [ "$rc" = 0 ] || { bad "exit=$rc: $out"; teardown; return; }
    ok "exit 0"
    [ ! -f "$T/stale-executed" ] && ok "stale first-run vps-apply.sh never executed" \
        || bad "STALE stub executed — re-run deployed the old stage"
    diff -q "$T/root/jerusalem/app.py" "$REPO/manager/app.py" >/dev/null \
        && ok "new stage's app.py deployed" || bad "app.py not from the new stage"
    grep -q 'bash /root/jerusalem-deploy/jerusalem-deploy-[0-9-]*[0-9]/vps-apply.sh' "$T/ssh.log" \
        && ok "ssh addressed the timestamped run dir" || bad "ssh did not address timestamped run dir"
    ndirs="$(ls -d "$T/root/jerusalem-deploy"/jerusalem-deploy-[0-9]*/ 2>/dev/null | wc -l)"
    [ "$ndirs" = 3 ] && ok "old run dirs pruned to 3" || bad "run dirs = $ndirs (want 3)"
    [ ! -d "$T/root/jerusalem-deploy/jerusalem-deploy-20000101-000000" ] \
        && [ ! -d "$T/root/jerusalem-deploy/jerusalem-deploy-20000102-000000" ] \
        && ok "the two oldest run dirs were pruned" || bad "oldest run dirs survived pruning"
    stagedir="$(echo "$out" | grep -o '/tmp/jerusalem-deploy-[0-9-]*' | head -1)"
    [ -n "$stagedir" ] && rm -rf "$stagedir"
    teardown
}

t_deploy_all_first_run_nested() {
    echo "test: deploy-all.sh first run (no /root/jerusalem-deploy) still executes the new stage"
    setup_fake
    write_fake_transport
    # /root/jerusalem-deploy NOT seeded: the mkdir -p must create it so scp
    # nests deterministically into $REMOTE_BASE/jerusalem-deploy-<ts>/
    out="$(FAKE_ROOT="$T" DF_SCP="$T/rbin/scp" DF_SSH="$T/rbin/ssh" SSH_OPTS="" \
        bash "$DEPLOY_ALL" fakehost --yes --skip-preflight 2>&1)"
    rc=$?
    [ "$rc" = 0 ] || { bad "exit=$rc: $out"; teardown; return; }
    ok "exit 0"
    diff -q "$T/root/jerusalem/app.py" "$REPO/manager/app.py" >/dev/null \
        && ok "new stage's app.py deployed" || bad "app.py not from the new stage"
    grep -q 'bash /root/jerusalem-deploy/jerusalem-deploy-[0-9-]*[0-9]/vps-apply.sh' "$T/ssh.log" \
        && ok "ssh addressed the timestamped run dir" || bad "ssh did not address timestamped run dir"
    stagedir="$(echo "$out" | grep -o '/tmp/jerusalem-deploy-[0-9-]*' | head -1)"
    [ -n "$stagedir" ] && rm -rf "$stagedir"
    teardown
}

t_happy_path
t_syntax_failure
t_restart_unhealthy_rollback
t_rollback_verify_failure
t_h264_501_warning
t_h264_404_rolls_back
t_extra_stage_file
t_missing_stage_file
t_state_file_guard
t_legacy_unit_guard
t_new_file_rollback_deletes
t_deploy_all_stage_only
t_deploy_all_rerun_fresh_stage
t_deploy_all_first_run_nested

echo "----"
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
