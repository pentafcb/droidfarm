#!/bin/bash
# vps-apply.sh — applies all staged Jerusalem 1.0 code deploys on the VPS.
#
# Runs ON the VPS as root. Copied there by deploy-all.sh (or manually).
# Applies, in one pass, every staged code deploy (full list in APPLY.md
# "What's included" — the header list used to drift, so it lives there now).
# All ride on the same six stage files: app.py / h264_stream.py / index.html /
#   agent.py / df-watchdog.sh / roblox-keepalive.sh — deployed from repo HEAD,
#   deduped; the per-package DEPLOY.md files remain as reference.
#
# Safety: timestamped backup of every overwritten file BEFORE install,
# python syntax check BEFORE restart, single restart, HTTP verification,
# automatic rollback (restore + restart + re-verify) on any post-install
# failure. Never touches state files (instances.json, roblox_accounts.json,
# tokens) — the stage allowlist is fixed and anything unexpected aborts.
#
# Env overrides (used by tests; defaults are the real VPS paths):
#   DF_DIR, STAGE_DIR, BACKUP_BASE, WATCHDOG_DST, WATCHDOG_LOG,
#   DEGRADED_DIR, KEEP_BACKUPS, SKIP_WATCHDOG_WAIT, DEPLOY_GIT_HASH
#
# Exit codes:
#   0  success (warnings allowed, printed as WARNING)
#   1  pre-deploy failure — nothing on the VPS was touched
#   2  deploy failure — rolled back to the backup
#   3  deploy failure AND rollback verification failed — MANUAL INTERVENTION
set -u

DF_DIR="${DF_DIR:-/root/jerusalem}"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
STAGE_DIR="${STAGE_DIR:-$SELF_DIR/stage}"
BACKUP_BASE="${BACKUP_BASE:-/root/jerusalem/.deploy-backup}"
WATCHDOG_DST="${WATCHDOG_DST:-/usr/local/bin/df-watchdog.sh}"
WATCHDOG_LOG="${WATCHDOG_LOG:-/var/log/df-watchdog.log}"
DEGRADED_DIR="${DEGRADED_DIR:-/tmp/df-watchdog}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"
SKIP_WATCHDOG_WAIT="${SKIP_WATCHDOG_WAIT:-0}"
DEPLOY_GIT_HASH="${DEPLOY_GIT_HASH:-unknown}"
PORT="${DROIDFARM_PORT:-8080}"

log()  { echo "[vps-apply] $*"; }
warn() { echo "[vps-apply] WARNING: $*" >&2; }
fail() { echo "[vps-apply] FAIL: $*" >&2; }

# Fixed stage allowlist: stage filename -> install target.
# Anything else in STAGE_DIR aborts the run (fail closed).
KEYS="app.py h264_stream.py index.html agent.py df-watchdog.sh roblox-keepalive.sh booted-watcher.sh repoint-backup-cron.sh screencap-loop.sh push-screencap.sh backup-state.sh verify-backup.sh finish-deploy.sh push-keepalive.sh push-booted-watcher.sh android-optimize.sh push-optimizer.sh install-wing.sh install-wing.bat"
target_for() {
    case "$1" in
        app.py)          echo "$DF_DIR/app.py" ;;
        h264_stream.py)  echo "$DF_DIR/h264_stream.py" ;;
        index.html)      echo "$DF_DIR/static/index.html" ;;
        agent.py)        echo "$DF_DIR/wing/agent.py" ;;
        install-wing.sh) echo "$DF_DIR/wing/install-wing.sh" ;;
        install-wing.bat) echo "$DF_DIR/wing/install-wing.bat" ;;
        df-watchdog.sh)  echo "$WATCHDOG_DST" ;;
        roblox-keepalive.sh) echo "$DF_DIR/roblox-keepalive.sh" ;;
        booted-watcher.sh)   echo "$DF_DIR/booted-watcher.sh" ;;
        repoint-backup-cron.sh) echo "$DF_DIR/repoint-backup-cron.sh" ;;
        screencap-loop.sh)   echo "$DF_DIR/screencap-loop.sh" ;;
        push-screencap.sh)   echo "$DF_DIR/push-screencap.sh" ;;
        backup-state.sh)    echo "$DF_DIR/backup-state.sh" ;;
        verify-backup.sh)   echo "$DF_DIR/verify-backup.sh" ;;
        finish-deploy.sh)   echo "$DF_DIR/finish-deploy.sh" ;;
        push-keepalive.sh)  echo "$DF_DIR/push-keepalive.sh" ;;
        push-booted-watcher.sh) echo "$DF_DIR/push-booted-watcher.sh" ;;
        android-optimize.sh) echo "$DF_DIR/android-optimize.sh" ;;
        push-optimizer.sh)  echo "$DF_DIR/push-optimizer.sh" ;;
        *)               echo "" ;;
    esac
}

BACKUP_DIR=""
RESTARTED=0          # set to 1 once we have restarted jerusalem post-install
NEWLY_CREATED=""     # targets that did not exist before install (rollback deletes them)

preflight() {
    [ -d "$STAGE_DIR" ] || { fail "stage dir missing: $STAGE_DIR"; return 1; }
    [ -d "$DF_DIR" ] || { fail "jerusalem dir missing: $DF_DIR"; return 1; }
    # The 2026-10-08 rename moved the manager from the legacy `droidfarm`
    # unit to `jerusalem`. If the old unit is somehow still ACTIVE it will
    # squat port 8080 and fight the new unit — fail closed with remediation
    # instead of deploying into a port conflict.
    if systemctl is-active --quiet droidfarm 2>/dev/null; then # legacy-guard: pre-rename unit must be gone
        fail "legacy droidfarm.service is ACTIVE (rename 2026-10-08 moved the manager to jerusalem.service); run: systemctl stop droidfarm && systemctl disable droidfarm"
        return 1
    fi
    for k in $KEYS; do
        [ -f "$STAGE_DIR/$k" ] || { fail "stage file missing: $k"; return 1; }
    done
    # Fail closed on unexpected stage contents (never deploy an unknown file,
    # and never let a state/credential file through even if allowlisted one day).
    for f in "$STAGE_DIR"/*; do
        base="$(basename "$f")"
        known=0
        for k in $KEYS; do [ "$base" = "$k" ] && known=1; done
        [ "$known" = 1 ] || { fail "unexpected file in stage dir: $base (aborting)"; return 1; }
        case "$base" in
            *instances.json*|*roblox_accounts*|*token*|*secret*|*password*|*.pem|*.key)
                fail "refusing to deploy suspicious filename: $base"; return 1 ;;
        esac
    done
    log "preflight ok: 19 staged files, jerusalem dir present"
    return 0
}

backup() {
    BACKUP_DIR="$BACKUP_BASE/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BACKUP_DIR" || { fail "cannot create $BACKUP_DIR"; return 1; }
    NEWLY_CREATED=""
    for k in $KEYS; do
        t="$(target_for "$k")"
        if [ -f "$t" ]; then
            cp -f "$t" "$BACKUP_DIR/$k" || { fail "backup of $t failed"; return 1; }
        else
            NEWLY_CREATED="$NEWLY_CREATED $k"
        fi
    done
    {
        echo "ts=$(date -u +%FT%TZ) git=$DEPLOY_GIT_HASH"
        echo "newly_created:$NEWLY_CREATED"
        (cd "$STAGE_DIR" && sha256sum $KEYS)
    } > "$BACKUP_DIR/MANIFEST"
    log "backed up to $BACKUP_DIR"
    return 0
}

install_files() {
    for k in $KEYS; do
        t="$(target_for "$k")"
        mkdir -p "$(dirname "$t")" || { fail "mkdir for $t"; return 1; }
        cp -f "$STAGE_DIR/$k" "$t" || { fail "install of $k -> $t"; return 1; }
    done
    log "installed 19 files"
    return 0
}

syntax_check() {
    python3 -m py_compile "$DF_DIR/app.py" 2>/tmp/vps-apply-pyerr || \
        { fail "py_compile app.py: $(head -3 /tmp/vps-apply-pyerr)"; return 1; }
    python3 -m py_compile "$DF_DIR/h264_stream.py" 2>/tmp/vps-apply-pyerr || \
        { fail "py_compile h264_stream.py: $(head -3 /tmp/vps-apply-pyerr)"; return 1; }
    python3 -m py_compile "$DF_DIR/wing/agent.py" 2>/tmp/vps-apply-pyerr || \
        { fail "py_compile agent.py: $(head -3 /tmp/vps-apply-pyerr)"; return 1; }
    bash -n "$DF_DIR/wing/install-wing.sh" || { fail "bash -n install-wing.sh"; return 1; }
    bash -n "$WATCHDOG_DST" || { fail "bash -n df-watchdog.sh"; return 1; }
    bash -n "$DF_DIR/repoint-backup-cron.sh" || { fail "bash -n repoint-backup-cron.sh"; return 1; }
    bash -n "$DF_DIR/screencap-loop.sh" || { fail "bash -n screencap-loop.sh"; return 1; }
    bash -n "$DF_DIR/push-screencap.sh" || { fail "bash -n push-screencap.sh"; return 1; }
    bash -n "$DF_DIR/backup-state.sh" || { fail "bash -n backup-state.sh"; return 1; }
    bash -n "$DF_DIR/verify-backup.sh" || { fail "bash -n verify-backup.sh"; return 1; }
    bash -n "$DF_DIR/finish-deploy.sh" || { fail "bash -n finish-deploy.sh"; return 1; }
    bash -n "$DF_DIR/push-keepalive.sh" || { fail "bash -n push-keepalive.sh"; return 1; }
    bash -n "$DF_DIR/push-booted-watcher.sh" || { fail "bash -n push-booted-watcher.sh"; return 1; }
    bash -n "$DF_DIR/android-optimize.sh" || { fail "bash -n android-optimize.sh"; return 1; }
    bash -n "$DF_DIR/push-optimizer.sh" || { fail "bash -n push-optimizer.sh"; return 1; }
    log "syntax ok (3x py_compile, bash -n)"
    return 0
}

ensure_ffmpeg() {
    if command -v ffmpeg >/dev/null 2>&1; then
        log "ffmpeg present: $(command -v ffmpeg)"
        return 0
    fi
    warn "ffmpeg missing — trying apt-get install (H.264 needs it; MJPEG fallback otherwise)"
    if apt-get install -y ffmpeg >/tmp/vps-apply-apt.log 2>&1 && command -v ffmpeg >/dev/null 2>&1; then
        log "ffmpeg installed"
        return 0
    fi
    warn "ffmpeg install failed — H.264 will 501 and the dashboard falls back to MJPEG"
    return 0  # warning only, not fatal
}

restart_service() {
    systemctl restart jerusalem || { fail "systemctl restart jerusalem"; return 1; }
    RESTARTED=1
    sleep 3
    service_healthy || { fail "jerusalem not healthy after restart"; return 1; }
    log "jerusalem restarted and healthy"
    return 0
}

# service_healthy: systemd reports active AND dashboard root answers 200.
service_healthy() {
    systemctl is-active jerusalem >/dev/null 2>&1 || return 1
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${PORT}/" 2>/dev/null)" || return 1
    [ "$code" = "200" ] || return 1
    return 0
}

# verify_service: dashboard root 200 (post-deploy check). Returns 0/1.
verify_service() {
    service_healthy || { fail "service unhealthy (is-active / HTTP 200 check)"; return 1; }
    log "service healthy: active + dashboard root 200"
    return 0
}

# verify_h264_route: 0 = 401 (route live, encoder available)
#                    3 = 501 (route live, encoder unavailable — warning path)
#                    2 = anything else (failure)
verify_h264_route() {
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${PORT}/ws/h264/rbx1" 2>/dev/null)" || return 2
    case "$code" in
        401) log "/ws/h264/rbx1 -> 401 (route live, auth enforced)"; return 0 ;;
        501) warn "/ws/h264/rbx1 -> 501 (route live, H.264 encoder unavailable — MJPEG fallback)"; return 3 ;;
        *)   fail "/ws/h264/rbx1 -> HTTP $code (want 401)"; return 2 ;;
    esac
}

check_banner() {
    if journalctl -u jerusalem --since "3 min ago" 2>/dev/null | grep -q "H.264: True"; then
        log "manager banner: H.264: True"
    else
        warn "manager banner does not show 'H.264: True' — check ffmpeg/Pillow on the VPS"
    fi
}

watchdog_check() {
    if [ "$SKIP_WATCHDOG_WAIT" = 1 ]; then
        log "watchdog observation skipped (SKIP_WATCHDOG_WAIT=1)"
        return 0
    fi
    log "observing watchdog for ~75s (one cron cycle)..."
    sleep 75
    if [ -f "$WATCHDOG_LOG" ]; then
        # fresh = written within the last 3 minutes
        if find "$WATCHDOG_LOG" -mmin -3 2>/dev/null | grep -q .; then
            log "watchdog log fresh:"
            tail -3 "$WATCHDOG_LOG" | sed 's/^/[vps-apply]   /'
        else
            warn "watchdog log not written in the last 3 min — cron may not have picked up v4"
        fi
    else
        warn "watchdog log $WATCHDOG_LOG not found"
    fi
    if ls "$DEGRADED_DIR"/*.degraded >/dev/null 2>&1; then
        warn "degraded markers present: $(ls "$DEGRADED_DIR"/*.degraded | tr '\n' ' ') — inspect, auto-recovery is paused for these"
    else
        log "no degraded markers"
    fi
    return 0
}

prune_backups() {
    # keep newest KEEP_BACKUPS backup dirs
    ls -1dt "$BACKUP_BASE"/*/ 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" | xargs -r rm -rf
    log "pruned backups (keeping $KEEP_BACKUPS)"
}

prune_old_runs() {
    # Each deploy-all.sh run uploads one timestamped dir under
    # /root/jerusalem-deploy/. Keep the newest 3, delete older ones so the
    # stage area can't grow without bound. Strict guards: only runs when this
    # script lives in .../jerusalem-deploy/jerusalem-deploy-<ts>/, and only
    # touches siblings matching the exact timestamp pattern. Warn-only:
    # pruning must never fail a deploy.
    local parent base d n
    parent="$(dirname "$SELF_DIR")"
    [ "$(basename "$parent")" = "jerusalem-deploy" ] || return 0
    base="$(basename "$SELF_DIR")"
    case "$base" in
        jerusalem-deploy-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
        *) return 0 ;;
    esac
    n=0
    for d in $(printf '%s\n' "$parent"/jerusalem-deploy-[0-9]*/ 2>/dev/null | sort -r); do
        d="${d%/}"
        [ -d "$d" ] || continue
        case "$(basename "$d")" in
            jerusalem-deploy-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
            *) continue ;;
        esac
        [ "$d" = "$SELF_DIR" ] && continue
        n=$((n + 1))
        [ "$n" -le 2 ] && continue
        rm -rf -- "$d" 2>/dev/null \
            && log "pruned old stage run: $(basename "$d")" \
            || warn "could not prune old stage run: $d"
    done
    return 0
}

rollback() {
    # $1 = reason. Restores backup; restarts only if we had restarted already.
    fail "ROLLBACK: $1"
    for k in $KEYS; do
        t="$(target_for "$k")"
        if [ -f "$BACKUP_DIR/$k" ]; then
            cp -f "$BACKUP_DIR/$k" "$t" && log "restored $k" || fail "restore of $k FAILED"
        else
            rm -f "$t" && log "removed newly-created $k"
        fi
    done
    if [ "$RESTARTED" = 1 ]; then
        systemctl restart jerusalem >/dev/null 2>&1
        sleep 3
        if service_healthy; then
            log "rollback complete: old code restored, service verified"
            return 2
        fi
        fail "ROLLBACK VERIFICATION FAILED — old code restored but service unhealthy. MANUAL INTERVENTION REQUIRED."
        return 3
    fi
    log "rollback complete: files restored (service was never restarted)"
    return 2
}

main() {
    log "applying staged deploys (git $DEPLOY_GIT_HASH)"
    preflight      || return 1
    backup         || return 1
    install_files  || { rollback "install failed"; return $?; }
    syntax_check   || { rollback "syntax check failed"; return $?; }
    ensure_ffmpeg
    restart_service || { rollback "restart failed"; return $?; }
    verify_service  || { rollback "service verification failed"; return $?; }
    verify_h264_route
    hrc=$?
    if [ "$hrc" = 2 ]; then
        rollback "h264 route verification failed"
        return $?
    fi
    # hrc 0 = full pass, 3 = warning path (route live, encoder missing) — continue
    check_banner
    watchdog_check
    prune_backups
    prune_old_runs
    if [ "$hrc" = 3 ]; then
        warn "deployed with warnings (H.264 encoder unavailable — dashboard falls back to MJPEG)"
    fi
    log "DONE: all staged deploys applied. Backup: $BACKUP_DIR"
    return 0
}

main
exit $?
