#!/bin/bash
# sync-newbox.sh — one-command code+baseline sync for the NEW VPS.
#
# The new VPS (64.177.49.68) runs the full Jerusalem 1.0 queue but is NOT the
# default apply-all target (deploy-all.sh defaults to the old VPS
# 209.222.10.93). Every new app.py/index.html/agent.py change would otherwise
# need a hand-rolled scp to the new box — this collapses the morning SSH
# session's new-box list into one verified command.
#
# Steps, in order, aborting on the first failure:
#   1. Code sync: deploy-all.sh <host> --yes — full preflight (queue audit,
#      all unit suites, dashboard JS gate, syntax), stage build from repo
#      HEAD, scp, then vps-apply.sh on the box (timestamped backup ->
#      install -> py_compile/bash -n gate -> single `systemctl restart
#      jerusalem` -> verify -> automatic rollback on failure). This is what
#      carries the binderfs modprobe self-heal that unblocks srt1–srt5
#      creation, and every other staged queue item.
#   2. Baseline: scp baseline-newbox.sh + its 3 sources (df-watchdog.sh,
#      backup-state.sh, verify-backup.sh) to the box and run it —
#      minute-cron watchdog, Docker log rotation, binder device count
#      surface, 03:17 daily state backup + integrity drill. Idempotent, safe
#      to re-run; installs only on sha256 difference.
#   3. verify-live.sh --host=<bare host> --ssh — file-level SHA check of all
#      19 staged files vs repo HEAD + jerusalem.service state + route probes.
#
# NOT in scope (need Penta's decisions / the old box / the APK upload):
# waydroid-image-build (Roblox APK upload + docker build on the new box),
# state-migrate (old-box backup archive import), creating the srt fleet, and
# decommissioning the old VPS.
#
# Usage: sync-newbox.sh [user@host] [--yes]
#   --yes   skip the confirmation prompt (deploy-all.sh gets --yes too)
# Env: SYNC_DEPLOY_ALL / SYNC_VERIFY_LIVE override the helper paths (tests);
#      DF_SCP / DF_SSH override the transfer commands (default scp/ssh);
#      SSH_OPTS default "-o BatchMode=yes -o ConnectTimeout=15".
# Exit: 0 all steps ok; 1 a step failed (per-step summary names it);
#       2 usage error.
# Never prints secrets: only hostnames, step names, and helper output.
set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOST="root@64.177.49.68"
ASSUME_YES=0
for a in "$@"; do
    case "$a" in
        --yes) ASSUME_YES=1 ;;
        -*) echo "[newbox-sync] unknown flag: $a" >&2; exit 2 ;;
        *) HOST="$a" ;;
    esac
done

DEPLOY_ALL="${SYNC_DEPLOY_ALL:-$REPO/pending-deploy/apply-all/deploy-all.sh}"
VERIFY_LIVE="${SYNC_VERIFY_LIVE:-$REPO/pending-deploy/live-verify/verify-live.sh}"
BASELINE_SRC="$REPO/pending-deploy/newbox-baseline/baseline-newbox.sh"
SCP="${DF_SCP:-scp}"
SSH="${DF_SSH:-ssh}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=15}"
REMOTE_BASE="/root/jerusalem-deploy"
BASELINE_REMOTE="$REMOTE_BASE/newbox-baseline"

# verify-live.sh --ssh expects a bare host (it prepends root@ itself).
BARE_HOST="${HOST#*@}"

log()  { echo "[newbox-sync] $*"; }
fail() { echo "[newbox-sync] FAIL: $*" >&2; }

STEP1=pending; STEP2=pending; STEP3=pending
summary() {
    log "summary: step1 code-sync=$STEP1, step2 baseline=$STEP2, step3 verify-live=$STEP3"
}

step1_code_sync() {
    log "step 1/3: code sync -> $HOST (deploy-all.sh full preflight + vps-apply.sh)"
    # shellcheck disable=SC2086
    "$DEPLOY_ALL" "$HOST" --yes || return 1
    STEP1=ok
    return 0
}

step2_baseline() {
    log "step 2/3: host baseline on $HOST (watchdog cron, log rotation, backup cron + drill)"
    # shellcheck disable=SC2086
    $SSH $SSH_OPTS "$HOST" "mkdir -p '$BASELINE_REMOTE'" || return 1
    # shellcheck disable=SC2086
    $SCP $SSH_OPTS \
        "$BASELINE_SRC" \
        "$REPO/waydroid-image/df-watchdog.sh" \
        "$REPO/pending-deploy/backup-state/backup-state.sh" \
        "$REPO/pending-deploy/backup-state/verify-backup.sh" \
        "$HOST:$BASELINE_REMOTE/" || return 1
    # shellcheck disable=SC2086
    $SSH $SSH_OPTS "$HOST" \
        "bash '$BASELINE_REMOTE/baseline-newbox.sh' '$BASELINE_REMOTE/df-watchdog.sh' '$BASELINE_REMOTE/backup-state.sh' '$BASELINE_REMOTE/verify-backup.sh'" \
        || return 1
    STEP2=ok
    return 0
}

step3_verify() {
    log "step 3/3: verify-live.sh --host=$BARE_HOST --ssh"
    "$VERIFY_LIVE" --host="$BARE_HOST" --ssh || return 1
    STEP3=ok
    return 0
}

main() {
    if [ "$ASSUME_YES" = 0 ]; then
        cat <<EOF
[newbox-sync] Will sync the new VPS ($HOST):
  1. deploy-all.sh $HOST --yes   (full preflight, stage, scp, vps-apply.sh with rollback)
  2. baseline-newbox.sh          (minute-cron watchdog, docker log rotation, backup cron + drill)
  3. verify-live.sh --host=$BARE_HOST --ssh
EOF
        printf "[newbox-sync] Proceed? [y/N] "
        read -r ans
        [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { fail "aborted by user"; exit 1; }
    fi
    step1_code_sync || { STEP1=FAILED; summary; fail "step 1 (code sync) failed — nothing else ran"; exit 1; }
    step2_baseline  || { STEP2=FAILED; summary; fail "step 2 (baseline) failed — code sync already applied, verify skipped"; exit 1; }
    step3_verify    || { STEP3=FAILED; summary; fail "step 3 (verify-live --ssh) failed — code+baseline applied, investigate the VERDICT above"; exit 1; }
    summary
    log "DONE: new VPS $HOST synced (code at $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown))."
    log "Still manual (need Penta / the APK upload): waydroid-image-build, state-migrate, Create fleet."
    return 0
}

main
