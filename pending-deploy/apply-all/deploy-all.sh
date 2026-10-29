#!/bin/bash
# deploy-all.sh — one-command deploy for every staged Jerusalem 1.0 code change.
#
# Runs on the machine that holds this repo (the worker VM). Does:
#   1. Preflight: working tree clean for deploy files, the queue-audit
#      wiring audit, all unit-test suites green (js-gate 10, h264 59,
#      remote-autologin 32, watchdog-v4 26, watchdog-v5 27, watchdog-v6 40,
#      remote-selfwatch 33, instance-health 17, watchdog-v7 11,
#      binderfs-selfheal 36, batch-ops 51, bulk-accounts 59,
#      login-observability 44, batch-login 40, fleet-create 71,
#      instance-resources 51, keepalive-v2 38, queue-audit 82,
#      login-hardening 33, login-ratelimit 34, watchdog-session 34,
#      remote-resources 43, log-trimmer 23, booted-watcher 23,
#      roblox-drift-badge 30, srt-version-drift 38, instance-health-cache 37,
#      stream-freshness 27, stream-frameless-guard 24, watchdog-events 37,
#      live-verify 119, thread-supervisor 36, backup-cron-repoint 23,
#      adaptive-screencap 47, node-disk-guard 54, watchdog-lock 22,
#      watchdog-v12 34, batch-journal 46, backup-state 40,
#      http-server-bounds 38, webrtc-stream-want 40, wing-failfast 43,
#      mjpeg-dedup 30, h264-skip-static 29, wing-selfwatch-dashboard 72,
#      webrtc-frameless-guard 32, post-deploy 84, waydroid-image-build 76,
#      newbox-baseline 53, state-migrate 59, newbox-sync 45,
#      android-optimizer 44,
#      create-preflight 22, fetch-roblox-apks 29, conn-banner 29,
#      binder-verify 26, cron-ensure 44, webrtc-loop-supervisor 39,
#      agent-thread-supervisor 29, batch-resume 59, batch-worker-safety 32,
#      webrtc-stranded-cleanup 26, backup-visibility 17+33,
#      wing-session-guard 49, loop-device-guard 30+25,
#      wing-loop-devices 49+16, wing-mem-guard 36, wing-stuck-boot 26,
#      wing-keepalive-mirror 35, wing-binderfs-selfheal 41,
#      wing-agent-selfcheck 71, wing-create-preflight 35,
#      remote-frame-cache 31, wing-auth-ratelimit 48,
#      wing-status-cache 28, remote-headroom 40, wing-agent-update 43,
#      remote-degraded 46, wing-selfwatch-lock 53, wing-agent-selfupdate 63,
#      route-name-gate 35, wing-route-name-gate 33, node-caps-visibility 33,
#      login-ratelimit-tunnel 34, wing-ratelimit-tunnel 38,
#      boot-complete 63, wing-missing-script-guard 35,
#      wing-installer-agent-check 20, wing-installer-staging 32,
#      login-username-escape 51, watchdog-timeout 50,
#      wing-update-rollback 53, install-baseline 27,
#      keepalive-probe-timeout 32, watcher-probe-timeout 26,
#      nodes-conn-err 25, boot-install-timeout 38, bulk-accounts-commas 24,
#      instance-resize 77, boot-recovery 50, wing-boot-recovery 85,
#      binder-safe-start 25, push-timeout 66, remote-status-invalidate 28,
#      wing-start-route-dedup 9, fleet-cpu-headroom 19,
#      roblox-install-guard 32, wing-install-guard 34,
#      resize-cache-invalidate 18, agent-body-cap 20, wing-update-path 20,
#      watchdog-liveness 11, webrtc-handshake-cleanup 34,
#      create-ip-gate 98, remote-input-b64 44, token-atomic-write 25,
#      login-pwd-cleanup 32, read-body-missing 20,
#      agent-ip-gate 66, remote-launch-fifo 19, auto-login-success 37,
#      login-script-cleanup 25, waydroid-exec-cleanup 33,
#      remote-b64-sweep 42, state-tmp-sweep 30, providers-600 18,
#      state-600-complete 33, login-script-600 33,
#      watchdog-daemon-guard 48, node-daemon-guard 55, global-health 44,
#      nodes-attention-badge 58, delete-state-sweep 45,
#      node-offline-badge 48,
#      fleet-resources-remote 54,
#      boot-recovery-badge 42,
#      watchdog-heartbeat-banner 18,
#      node-fetch-badge 62,
#      mjpeg-terminal-guard 35,
#      single-restart-route 30,
#      stale-frame-gate 35,
#      launch-focus-gate 35,
#      remote-fetch-exception-cache 27,
#      ws-reader-timeout 28,
#      h264-init-pin 23,
#      missing-frame-guard 27,
#      input-coord-validation 38,
#      numeric-overflow-guard 43,
#      non-dict-json-guard 39,
#      request-type-guard 32,
#      state-shape-guard 33,
#      state-load-robustness 40,
#      provider-record-guard 60,
#      instance-record-guard 49,
#      account-record-guard 34,
#      route-auth-census 78,
#      static-path-guard 20,
#      dashboard-html-escape 12,
#      watchdog-name-gate 21,
#      login-name-gate 34,
#      create-sink-gate 47,
#      resize-sink-gate 45,
#      action-sink-gate 53),
#      screenshot-path-gate 29,
#      input-sink-gate 44,
#      autologin-probe-gate 64,
#      remote-resize-sink-gate 49,
#      remote-screenshot-sink-gate 34),
#      agent-status-delete-sink-gate 121,
#      agent-create-numerics-sink-gate 89,
#      agent-resize-numerics-sink-gate 101,
#      agent-binder-count-sink-gate 148,
#      agent-sw-numerics-sink-gate 96),
#      syntax checks (py_compile,
#      bash -n, and the dashboard JS syntax gate — node --check over every
#      inline <script> block, fail-closed).
#   2. Builds a stage dir (19 files + vps-apply.sh + MANIFEST with git hash).
#   3. mkdir -p /root/jerusalem-deploy on the VPS, then scp -r the stage dir
#      into it (the mkdir keeps the layout deterministic: scp -r nests into
#      an existing dir but flattens onto a missing one, so without it a
#      re-run would execute the FIRST run's vps-apply.sh + stage).
#   4. Runs the just-uploaded vps-apply.sh on the VPS over ssh
#      ($REMOTE_BASE/jerusalem-deploy-<ts>/vps-apply.sh: backup -> install ->
#      verify, automatic rollback on failure; old run dirs pruned to 3).
#
# What it deploys (deduped across the pending packages):
#   manager/h264_stream.py  -> /root/jerusalem/h264_stream.py      (h264-mse + stream-frameless-guard + h264-skip-static)
#   manager/app.py          -> /root/jerusalem/app.py              (h264-mse + remote-autologin + remote-selfwatch
#                                                                   + instance-health + binderfs-selfheal + batch-ops
#                                                                   + bulk-accounts + login-observability + batch-login
#                                                                   + fleet-create + instance-resources + login-hardening
#                                                                   + login-ratelimit + remote-resources
#                                                                   + roblox-drift-badge + srt-version-drift
#                                                                   + instance-health-cache + stream-frameless-guard
#                                                                   + adaptive-screencap + http-server-bounds
#                                                                   + webrtc-stream-want + wing-failfast
#                                                                   + mjpeg-dedup + create-preflight)
#   manager/static/index.html -> /root/jerusalem/static/index.html (h264-mse + remote-autologin + webrtc-signaling-fix
#                                                                   + remote-selfwatch + instance-health + batch-ops
#                                                                   + bulk-accounts + login-observability + batch-login
#                                                                   + fleet-create + instance-resources + login-hardening
#                                                                   + login-ratelimit + remote-resources
#                                                                   + booted-watcher + roblox-drift-badge + stream-freshness
#                                                                   + mjpeg-dedup)
#   wing/agent.py           -> /root/jerusalem/wing/agent.py       (remote-autologin + remote-selfwatch + remote-resources
#                                                                   + srt-version-drift + adaptive-screencap
#                                                                   + node-disk-guard + http-server-bounds
#                                                                   + wing-session-guard)
#   wing/install-wing.sh    -> /root/jerusalem/wing/install-wing.sh (wing-installer-staging + wing-installer-agent-check;
#                                                                   the Linux node installer, served from disk per
#                                                                   request — placeholders replaced at serve time)
#   wing/install-wing.bat   -> /root/jerusalem/wing/install-wing.bat (wing-installer-staging; the Windows node
#                                                                   installer, served from disk per request)
#   waydroid-image/df-watchdog.sh -> /usr/local/bin/df-watchdog.sh (watchdog v4-v12
#                                                                     + instance-health + keepalive-v2 surfacing
#                                                                     + log-trimmer section 10
#                                                                     + roblox-drift-badge)
#   waydroid-image/roblox-keepalive.sh -> /root/jerusalem/roblox-keepalive.sh (keepalive-v2;
#                                                                   push-keepalive.sh then pushes it into live containers)
#   waydroid-image/booted-watcher.sh -> /root/jerusalem/booted-watcher.sh (booted-watcher;
#                                                                   push-booted-watcher.sh then starts it in live containers)
#   pending-deploy/backup-cron-repoint/repoint-backup-cron.sh -> /root/jerusalem/repoint-backup-cron.sh
#                                                                   (backup-cron-repoint; staged only — run by hand once
#                                                                   on the VPS to repoint the 03:17 backup cron)
#   waydroid-image/screencap-loop.sh -> /root/jerusalem/screencap-loop.sh (adaptive-screencap;
#                                                                   canonical on-VPS source for the push below)
#   pending-deploy/adaptive-screencap/push-screencap.sh -> /root/jerusalem/push-screencap.sh
#                                                                   (adaptive-screencap; staged only — run by hand once
#                                                                   on the VPS to push the loop into live containers)
#   pending-deploy/backup-state/backup-state.sh -> /root/jerusalem/backup-state.sh
#                                                                   (backup-state; staged only — the new 03:17 daily
#                                                                   state backup; replace the old cron line by hand)
#   pending-deploy/backup-state/verify-backup.sh -> /root/jerusalem/verify-backup.sh
#                                                                   (backup-state; staged only — archive integrity
#                                                                   drill tool, run by hand)
#   pending-deploy/post-deploy/finish-deploy.sh -> /root/jerusalem/finish-deploy.sh
#                                                                   (post-deploy; staged only — run by hand once
#                                                                   on the VPS AFTER this deploy: runs all seven
#                                                                   post-deploy steps in order — the four live
#                                                                   container pushes, the backup-cron repoint, the
#                                                                   backup cron-line switch, and the backup run +
#                                                                   drill; aborts if any staged push script is
#                                                                   missing — queue-audit check 9 guards this)
#   pending-deploy/keepalive-v2/push-keepalive.sh -> /root/jerusalem/push-keepalive.sh
#                                                                   (keepalive-v2; staged only — finish-deploy.sh
#                                                                   step 1 pushes it into live containers)
#   pending-deploy/booted-watcher/push-booted-watcher.sh -> /root/jerusalem/push-booted-watcher.sh
#                                                                   (booted-watcher; staged only — finish-deploy.sh
#                                                                   step 2 pushes it into live containers)
#   waydroid-image/android-optimize.sh -> /root/jerusalem/android-optimize.sh
#                                                                   (android-optimizer; canonical on-VPS source
#                                                                   for the push below)
#   pending-deploy/android-optimizer/push-optimizer.sh -> /root/jerusalem/push-optimizer.sh
#                                                                   (android-optimizer; staged only — finish-deploy.sh
#                                                                   step 4 pushes it into live containers)
#
# NOT included (separate manual steps, see APPLY.md):
#   python-security-patch (apt on BOTH boxes), vultr agent 1.0.2 reinstall,
#   waydroid-instance docker image rebuild (entrypoint 0.12s loop).
#
# Usage: deploy-all.sh [user@host] [--stage-only] [--yes] [--skip-preflight]
#   --stage-only   build the stage dir and print scp/ssh commands, don't run them
#   --yes          skip the confirmation prompt
# Env: DF_SCP / DF_SSH override the transfer commands (default scp/ssh);
#      SSH_OPTS default "-o BatchMode=yes -o ConnectTimeout=15".
set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
VPS="root@209.222.10.93"
STAGE_ONLY=0
ASSUME_YES=0
SKIP_PREFLIGHT=0
for a in "$@"; do
    case "$a" in
        --stage-only) STAGE_ONLY=1 ;;
        --yes) ASSUME_YES=1 ;;
        --skip-preflight) SKIP_PREFLIGHT=1 ;;
        -*) echo "unknown flag: $a" >&2; exit 1 ;;
        *) VPS="$a" ;;
    esac
done

SCP="${DF_SCP:-scp}"
SSH="${DF_SSH:-ssh}"
SSH_OPTS="${SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=15}"
REMOTE_BASE="/root/jerusalem-deploy"

log()  { echo "[deploy-all] $*"; }
fail() { echo "[deploy-all] FAIL: $*" >&2; exit 1; }

# repo-relative source -> stage filename
SOURCES="manager/h264_stream.py manager/app.py manager/static/index.html wing/agent.py waydroid-image/df-watchdog.sh waydroid-image/roblox-keepalive.sh waydroid-image/booted-watcher.sh pending-deploy/backup-cron-repoint/repoint-backup-cron.sh waydroid-image/screencap-loop.sh pending-deploy/adaptive-screencap/push-screencap.sh pending-deploy/backup-state/backup-state.sh pending-deploy/backup-state/verify-backup.sh pending-deploy/post-deploy/finish-deploy.sh pending-deploy/keepalive-v2/push-keepalive.sh pending-deploy/booted-watcher/push-booted-watcher.sh waydroid-image/android-optimize.sh pending-deploy/android-optimizer/push-optimizer.sh wing/install-wing.sh wing/install-wing.bat"
STAGE_NAMES="h264_stream.py app.py index.html agent.py df-watchdog.sh roblox-keepalive.sh booted-watcher.sh repoint-backup-cron.sh screencap-loop.sh push-screencap.sh backup-state.sh verify-backup.sh finish-deploy.sh push-keepalive.sh push-booted-watcher.sh android-optimize.sh push-optimizer.sh install-wing.sh install-wing.bat"

preflight() {
    log "preflight: git state"
    dirty="$(git -C "$REPO" status --porcelain -- $SOURCES pending-deploy/apply-all/ 2>/dev/null)"
    [ -z "$dirty" ] || fail "working tree dirty for deploy files (commit first):$'\n'$dirty"
    GIT_HASH="$(git -C "$REPO" rev-parse --short HEAD)"
    log "git HEAD: $GIT_HASH (clean)"

    log "preflight: queue audit"
    bash "$REPO/pending-deploy/queue-audit/audit-queue.sh" \
        || fail "queue audit failed (wiring drift — see FAIL lines above)"

    log "preflight: unit tests"
    bash "$REPO/pending-deploy/apply-all/test_js_gate.sh" > /tmp/deploy-all-t20.log 2>&1 \
        || fail "js-gate tests failed (see /tmp/deploy-all-t20.log)"
    grep -q "PASS=10 FAIL=0" /tmp/deploy-all-t20.log || fail "js-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/h264-mse/test_h264_integration.py" > /tmp/deploy-all-t1.log 2>&1 \
        || fail "h264 tests failed (see /tmp/deploy-all-t1.log)"
    tail -1 /tmp/deploy-all-t1.log | grep -q "59 passed" || fail "h264 tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-autologin/test_remote_autologin.py" > /tmp/deploy-all-t2.log 2>&1 \
        || fail "remote-autologin tests failed (see /tmp/deploy-all-t2.log)"
    tail -1 /tmp/deploy-all-t2.log | grep -q "32 passed" || fail "remote-autologin tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-v4/test_watchdog_v4.sh" > /tmp/deploy-all-t3.log 2>&1 \
        || fail "watchdog tests failed (see /tmp/deploy-all-t3.log)"
    grep -q "PASS=26 FAIL=0" /tmp/deploy-all-t3.log || fail "watchdog tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-v5/test_watchdog_v5.sh" > /tmp/deploy-all-t6.log 2>&1 \
        || fail "watchdog-v5 tests failed (see /tmp/deploy-all-t6.log)"
    grep -q "PASS=27 FAIL=0" /tmp/deploy-all-t6.log || fail "watchdog-v5 tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-v6/test_watchdog_v6.sh" > /tmp/deploy-all-t7.log 2>&1 \
        || fail "watchdog-v6 tests failed (see /tmp/deploy-all-t7.log)"
    grep -q "PASS=40 FAIL=0" /tmp/deploy-all-t7.log || fail "watchdog-v6 tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-selfwatch/test_agent_selfwatch.py" > /tmp/deploy-all-t4.log 2>&1 \
        || fail "selfwatch tests failed (see /tmp/deploy-all-t4.log)"
    tail -1 /tmp/deploy-all-t4.log | grep -q "PASS=33 FAIL=0" || fail "selfwatch tests: unexpected result"
    python3 "$REPO/pending-deploy/instance-health/test_instance_health.py" > /tmp/deploy-all-t5.log 2>&1 \
        || fail "instance-health tests failed (see /tmp/deploy-all-t5.log)"
    tail -1 /tmp/deploy-all-t5.log | grep -q "17 passed, 0 failed" || fail "instance-health tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-v7/test_watchdog_v7.sh" > /tmp/deploy-all-t8.log 2>&1 \
        || fail "watchdog-v7 tests failed (see /tmp/deploy-all-t8.log)"
    grep -q "PASS=11 FAIL=0" /tmp/deploy-all-t8.log || fail "watchdog-v7 tests: unexpected result"
    python3 "$REPO/pending-deploy/binderfs-selfheal/test_binderfs_selfheal.py" > /tmp/deploy-all-t9.log 2>&1 \
        || fail "binderfs-selfheal tests failed (see /tmp/deploy-all-t9.log)"
    tail -1 /tmp/deploy-all-t9.log | grep -q "PASS=36 FAIL=0" || fail "binderfs-selfheal tests: unexpected result"
    python3 "$REPO/pending-deploy/batch-ops/test_batch_ops.py" > /tmp/deploy-all-t10.log 2>&1 \
        || fail "batch-ops tests failed (see /tmp/deploy-all-t10.log)"
    tail -1 /tmp/deploy-all-t10.log | grep -q "51 passed, 0 failed" || fail "batch-ops tests: unexpected result"
    python3 "$REPO/pending-deploy/bulk-accounts/test_bulk_accounts.py" > /tmp/deploy-all-t11.log 2>&1 \
        || fail "bulk-accounts tests failed (see /tmp/deploy-all-t11.log)"
    tail -1 /tmp/deploy-all-t11.log | grep -q "59 passed, 0 failed" || fail "bulk-accounts tests: unexpected result"
    python3 "$REPO/pending-deploy/login-observability/test_login_observability.py" > /tmp/deploy-all-t12.log 2>&1 \
        || fail "login-observability tests failed (see /tmp/deploy-all-t12.log)"
    tail -1 /tmp/deploy-all-t12.log | grep -q "44 passed, 0 failed" || fail "login-observability tests: unexpected result"
    python3 "$REPO/pending-deploy/batch-login/test_batch_login.py" > /tmp/deploy-all-t13.log 2>&1 \
        || fail "batch-login tests failed (see /tmp/deploy-all-t13.log)"
    tail -1 /tmp/deploy-all-t13.log | grep -q "40 passed, 0 failed" || fail "batch-login tests: unexpected result"
    python3 "$REPO/pending-deploy/fleet-create/test_fleet_create.py" > /tmp/deploy-all-t14.log 2>&1 \
        || fail "fleet-create tests failed (see /tmp/deploy-all-t14.log)"
    tail -1 /tmp/deploy-all-t14.log | grep -q "71 passed, 0 failed" || fail "fleet-create tests: unexpected result"
    python3 "$REPO/pending-deploy/instance-resources/test_instance_resources.py" > /tmp/deploy-all-t15.log 2>&1 \
        || fail "instance-resources tests failed (see /tmp/deploy-all-t15.log)"
    tail -1 /tmp/deploy-all-t15.log | grep -q "51 passed, 0 failed" || fail "instance-resources tests: unexpected result"
    bash "$REPO/pending-deploy/keepalive-v2/test_keepalive_v2.sh" > /tmp/deploy-all-t16.log 2>&1 \
        || fail "keepalive-v2 tests failed (see /tmp/deploy-all-t16.log)"
    tail -1 /tmp/deploy-all-t16.log | grep -q "38 passed, 0 failed" || fail "keepalive-v2 tests: unexpected result"
    python3 "$REPO/pending-deploy/queue-audit/test_queue_audit.py" > /tmp/deploy-all-t17.log 2>&1 \
        || fail "queue-audit tests failed (see /tmp/deploy-all-t17.log)"
    tail -1 /tmp/deploy-all-t17.log | grep -q "82 passed, 0 failed" || fail "queue-audit tests: unexpected result"
    python3 "$REPO/pending-deploy/login-hardening/test_login_hardening.py" > /tmp/deploy-all-t18.log 2>&1 \
        || fail "login-hardening tests failed (see /tmp/deploy-all-t18.log)"
    tail -1 /tmp/deploy-all-t18.log | grep -q "33 passed, 0 failed" || fail "login-hardening tests: unexpected result"
    python3 "$REPO/pending-deploy/login-ratelimit/test_login_ratelimit.py" > /tmp/deploy-all-t19.log 2>&1 \
        || fail "login-ratelimit tests failed (see /tmp/deploy-all-t19.log)"
    tail -1 /tmp/deploy-all-t19.log | grep -q "34 passed, 0 failed" || fail "login-ratelimit tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-session/test_watchdog_v9.sh" > /tmp/deploy-all-t21.log 2>&1 \
        || fail "watchdog-session tests failed (see /tmp/deploy-all-t21.log)"
    grep -q "PASS=34 FAIL=0" /tmp/deploy-all-t21.log || fail "watchdog-session tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-resources/test_remote_resources.py" > /tmp/deploy-all-t22.log 2>&1 \
        || fail "remote-resources tests failed (see /tmp/deploy-all-t22.log)"
    tail -1 /tmp/deploy-all-t22.log | grep -q "43 passed, 0 failed" || fail "remote-resources tests: unexpected result"
    bash "$REPO/pending-deploy/log-trimmer/test_log_trimmer.sh" > /tmp/deploy-all-t23.log 2>&1 \
        || fail "log-trimmer tests failed (see /tmp/deploy-all-t23.log)"
    grep -q "PASS=23 FAIL=0" /tmp/deploy-all-t23.log || fail "log-trimmer tests: unexpected result"
    bash "$REPO/pending-deploy/booted-watcher/test_booted_watcher.sh" > /tmp/deploy-all-t24.log 2>&1 \
        || fail "booted-watcher tests failed (see /tmp/deploy-all-t24.log)"
    tail -1 /tmp/deploy-all-t24.log | grep -q "23 passed, 0 failed" || fail "booted-watcher tests: unexpected result"
    python3 "$REPO/pending-deploy/roblox-drift-badge/test_roblox_drift_badge.py" > /tmp/deploy-all-t26.log 2>&1 \
        || fail "roblox-drift-badge tests failed (see /tmp/deploy-all-t26.log)"
    tail -1 /tmp/deploy-all-t26.log | grep -q "30 passed, 0 failed" || fail "roblox-drift-badge tests: unexpected result"
    python3 "$REPO/pending-deploy/srt-version-drift/test_srt_version_drift.py" > /tmp/deploy-all-t27.log 2>&1 \
        || fail "srt-version-drift tests failed (see /tmp/deploy-all-t27.log)"
    tail -1 /tmp/deploy-all-t27.log | grep -q "38 passed, 0 failed" || fail "srt-version-drift tests: unexpected result"
    python3 "$REPO/pending-deploy/instance-health-cache/test_instance_health_cache.py" > /tmp/deploy-all-t28.log 2>&1 \
        || fail "instance-health-cache tests failed (see /tmp/deploy-all-t28.log)"
    tail -1 /tmp/deploy-all-t28.log | grep -q "37 passed, 0 failed" || fail "instance-health-cache tests: unexpected result"
    python3 "$REPO/pending-deploy/stream-freshness/test_stream_freshness.py" > /tmp/deploy-all-t29.log 2>&1 \
        || fail "stream-freshness tests failed (see /tmp/deploy-all-t29.log)"
    tail -1 /tmp/deploy-all-t29.log | grep -q "27 passed, 0 failed" || fail "stream-freshness tests: unexpected result"
    python3 "$REPO/pending-deploy/stream-frameless-guard/test_stream_frameless_guard.py" > /tmp/deploy-all-t30.log 2>&1 \
        || fail "stream-frameless-guard tests failed (see /tmp/deploy-all-t30.log)"
    tail -1 /tmp/deploy-all-t30.log | grep -q "24 passed, 0 failed" || fail "stream-frameless-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/watchdog-events/test_watchdog_events.py" > /tmp/deploy-all-t31.log 2>&1 \
        || fail "watchdog-events tests failed (see /tmp/deploy-all-t31.log)"
    tail -1 /tmp/deploy-all-t31.log | grep -q "37 passed, 0 failed" || fail "watchdog-events tests: unexpected result"
    bash "$REPO/pending-deploy/live-verify/test_live_verify.sh" > /tmp/deploy-all-t32.log 2>&1 \
        || fail "live-verify tests failed (see /tmp/deploy-all-t32.log)"
    grep -q "PASS=119 FAIL=0" /tmp/deploy-all-t32.log || fail "live-verify tests: unexpected result"
    python3 "$REPO/pending-deploy/thread-supervisor/test_thread_supervisor.py" > /tmp/deploy-all-t33.log 2>&1 \
        || fail "thread-supervisor tests failed (see /tmp/deploy-all-t33.log)"
    tail -1 /tmp/deploy-all-t33.log | grep -q "36 passed, 0 failed" || fail "thread-supervisor tests: unexpected result"
    bash "$REPO/pending-deploy/backup-cron-repoint/test_backup_cron_repoint.sh" > /tmp/deploy-all-t34.log 2>&1 \
        || fail "backup-cron-repoint tests failed (see /tmp/deploy-all-t34.log)"
    tail -1 /tmp/deploy-all-t34.log | grep -q "23 passed, 0 failed" || fail "backup-cron-repoint tests: unexpected result"
    python3 "$REPO/pending-deploy/adaptive-screencap/test_adaptive_screencap.py" > /tmp/deploy-all-t35.log 2>&1 \
        || fail "adaptive-screencap tests failed (see /tmp/deploy-all-t35.log)"
    tail -1 /tmp/deploy-all-t35.log | grep -q "43 passed, 0 failed" || fail "adaptive-screencap tests: unexpected result"
    bash "$REPO/pending-deploy/adaptive-screencap/test_screencap_loop.sh" > /tmp/deploy-all-t35b.log 2>&1 \
        || fail "adaptive-screencap loop tests failed (see /tmp/deploy-all-t35b.log)"
    tail -1 /tmp/deploy-all-t35b.log | grep -q "5 passed, 0 failed" || fail "adaptive-screencap loop tests: unexpected result"
    python3 "$REPO/pending-deploy/node-disk-guard/test_node_disk_guard.py" > /tmp/deploy-all-t36.log 2>&1 \
        || fail "node-disk-guard tests failed (see /tmp/deploy-all-t36.log)"
    tail -1 /tmp/deploy-all-t36.log | grep -q "54 passed, 0 failed" || fail "node-disk-guard tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-lock/test_watchdog_lock.sh" > /tmp/deploy-all-t37.log 2>&1 \
        || fail "watchdog-lock tests failed (see /tmp/deploy-all-t37.log)"
    tail -1 /tmp/deploy-all-t37.log | grep -q "22 passed, 0 failed" || fail "watchdog-lock tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-v12/test_watchdog_v12.sh" > /tmp/deploy-all-t38.log 2>&1 \
        || fail "watchdog-v12 tests failed (see /tmp/deploy-all-t38.log)"
    tail -1 /tmp/deploy-all-t38.log | grep -q "34 passed, 0 failed" || fail "watchdog-v12 tests: unexpected result"
    python3 "$REPO/pending-deploy/batch-journal/test_batch_journal.py" > /tmp/deploy-all-t39.log 2>&1 \
        || fail "batch-journal tests failed (see /tmp/deploy-all-t39.log)"
    tail -1 /tmp/deploy-all-t39.log | grep -q "46 passed, 0 failed" || fail "batch-journal tests: unexpected result"
    bash "$REPO/pending-deploy/backup-state/test_backup_state.sh" > /tmp/deploy-all-t40.log 2>&1 \
        || fail "backup-state tests failed (see /tmp/deploy-all-t40.log)"
    tail -1 /tmp/deploy-all-t40.log | grep -q "40 passed, 0 failed" || fail "backup-state tests: unexpected result"
    python3 "$REPO/pending-deploy/http-server-bounds/test_http_server_bounds.py" > /tmp/deploy-all-t41.log 2>&1 \
        || fail "http-server-bounds tests failed (see /tmp/deploy-all-t41.log)"
    tail -1 /tmp/deploy-all-t41.log | grep -q "38 passed, 0 failed" || fail "http-server-bounds tests: unexpected result"
    python3 "$REPO/pending-deploy/webrtc-stream-want/test_webrtc_stream_want.py" > /tmp/deploy-all-t42.log 2>&1 \
        || fail "webrtc-stream-want tests failed (see /tmp/deploy-all-t42.log)"
    tail -1 /tmp/deploy-all-t42.log | grep -q "40 passed, 0 failed" || fail "webrtc-stream-want tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-failfast/test_wing_failfast.py" > /tmp/deploy-all-t43.log 2>&1 \
        || fail "wing-failfast tests failed (see /tmp/deploy-all-t43.log)"
    tail -1 /tmp/deploy-all-t43.log | grep -q "43 passed, 0 failed" || fail "wing-failfast tests: unexpected result"
    python3 "$REPO/pending-deploy/mjpeg-dedup/test_mjpeg_dedup.py" > /tmp/deploy-all-t44.log 2>&1 \
        || fail "mjpeg-dedup tests failed (see /tmp/deploy-all-t44.log)"
    tail -1 /tmp/deploy-all-t44.log | grep -q "30 passed, 0 failed" || fail "mjpeg-dedup tests: unexpected result"
    python3 "$REPO/pending-deploy/h264-skip-static/test_h264_skip_static.py" > /tmp/deploy-all-t45.log 2>&1 \
        || fail "h264-skip-static tests failed (see /tmp/deploy-all-t45.log)"
    tail -1 /tmp/deploy-all-t45.log | grep -q "29 passed, 0 failed" || fail "h264-skip-static tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-selfwatch-dashboard/test_wing_selfwatch_dashboard.py" > /tmp/deploy-all-t46.log 2>&1 \
        || fail "wing-selfwatch-dashboard tests failed (see /tmp/deploy-all-t46.log)"
    tail -1 /tmp/deploy-all-t46.log | grep -q "72 passed, 0 failed" || fail "wing-selfwatch-dashboard tests: unexpected result"
    python3 "$REPO/pending-deploy/webrtc-frameless-guard/test_webrtc_frameless_guard.py" > /tmp/deploy-all-t47.log 2>&1 \
        || fail "webrtc-frameless-guard tests failed (see /tmp/deploy-all-t47.log)"
    tail -1 /tmp/deploy-all-t47.log | grep -q "32 passed, 0 failed" || fail "webrtc-frameless-guard tests: unexpected result"
    bash "$REPO/pending-deploy/post-deploy/test_post_deploy.sh" > /tmp/deploy-all-t48.log 2>&1 \
        || fail "post-deploy tests failed (see /tmp/deploy-all-t48.log)"
    tail -1 /tmp/deploy-all-t48.log | grep -q "84 passed, 0 failed" || fail "post-deploy tests: unexpected result"
    bash "$REPO/pending-deploy/waydroid-image-build/test_waydroid_image_build.sh" > /tmp/deploy-all-t49.log 2>&1 \
        || fail "waydroid-image-build tests failed (see /tmp/deploy-all-t49.log)"
    tail -1 /tmp/deploy-all-t49.log | grep -q "76 passed, 0 failed" || fail "waydroid-image-build tests: unexpected result"
    bash "$REPO/pending-deploy/newbox-baseline/test_newbox_baseline.sh" > /tmp/deploy-all-t50.log 2>&1 \
        || fail "newbox-baseline tests failed (see /tmp/deploy-all-t50.log)"
    tail -1 /tmp/deploy-all-t50.log | grep -q "53 passed, 0 failed" || fail "newbox-baseline tests: unexpected result"
    bash "$REPO/pending-deploy/state-migrate/test_state_migrate.sh" > /tmp/deploy-all-t51.log 2>&1 \
        || fail "state-migrate tests failed (see /tmp/deploy-all-t51.log)"
    tail -1 /tmp/deploy-all-t51.log | grep -q "59 passed, 0 failed" || fail "state-migrate tests: unexpected result"
    bash "$REPO/pending-deploy/newbox-sync/test_newbox_sync.sh" > /tmp/deploy-all-t52.log 2>&1 \
        || fail "newbox-sync tests failed (see /tmp/deploy-all-t52.log)"
    grep -q "PASS=45 FAIL=0" /tmp/deploy-all-t52.log || fail "newbox-sync tests: unexpected result"
    bash "$REPO/pending-deploy/android-optimizer/test_android_optimizer.sh" > /tmp/deploy-all-t53.log 2>&1 \
        || fail "android-optimizer tests failed (see /tmp/deploy-all-t53.log)"
    tail -1 /tmp/deploy-all-t53.log | grep -q "45 passed, 0 failed" || fail "android-optimizer tests: unexpected result"
    python3 "$REPO/pending-deploy/create-preflight/test_create_preflight.py" > /tmp/deploy-all-t54.log 2>&1 \
        || fail "create-preflight tests failed (see /tmp/deploy-all-t54.log)"
    tail -1 /tmp/deploy-all-t54.log | grep -q "22 passed, 0 failed" || fail "create-preflight tests: unexpected result"
    bash "$REPO/pending-deploy/waydroid-image-build/test_fetch_roblox_apks.sh" > /tmp/deploy-all-t55.log 2>&1 \
        || fail "fetch-roblox-apks tests failed (see /tmp/deploy-all-t55.log)"
    tail -1 /tmp/deploy-all-t55.log | grep -q "29 passed, 0 failed" || fail "fetch-roblox-apks tests: unexpected result"
    python3 "$REPO/pending-deploy/conn-banner/test_conn_banner.py" > /tmp/deploy-all-t56.log 2>&1 \
        || fail "conn-banner tests failed (see /tmp/deploy-all-t56.log)"
    tail -1 /tmp/deploy-all-t56.log | grep -q "29 passed, 0 failed" || fail "conn-banner tests: unexpected result"
    bash "$REPO/pending-deploy/binder-verify/test_verify_binder.sh" > /tmp/deploy-all-t57.log 2>&1 \
        || fail "binder-verify tests failed (see /tmp/deploy-all-t57.log)"
    tail -1 /tmp/deploy-all-t57.log | grep -q "26 passed, 0 failed" || fail "binder-verify tests: unexpected result"
    bash "$REPO/pending-deploy/cron-ensure/test_cron_ensure.sh" > /tmp/deploy-all-t58.log 2>&1 \
        || fail "cron-ensure tests failed (see /tmp/deploy-all-t58.log)"
    tail -1 /tmp/deploy-all-t58.log | grep -q "44 passed, 0 failed" || fail "cron-ensure tests: unexpected result"
    python3 "$REPO/pending-deploy/webrtc-loop-supervisor/test_webrtc_loop_supervisor.py" > /tmp/deploy-all-t59.log 2>&1 \
        || fail "webrtc-loop-supervisor tests failed (see /tmp/deploy-all-t59.log)"
    tail -1 /tmp/deploy-all-t59.log | grep -q "39 passed, 0 failed" || fail "webrtc-loop-supervisor tests: unexpected result"
    python3 "$REPO/pending-deploy/agent-thread-supervisor/test_agent_thread_supervisor.py" > /tmp/deploy-all-t60.log 2>&1 \
        || fail "agent-thread-supervisor tests failed (see /tmp/deploy-all-t60.log)"
    tail -1 /tmp/deploy-all-t60.log | grep -q "29 passed, 0 failed" || fail "agent-thread-supervisor tests: unexpected result"
    python3 "$REPO/pending-deploy/batch-resume/test_batch_resume.py" > /tmp/deploy-all-t61.log 2>&1 \
        || fail "batch-resume tests failed (see /tmp/deploy-all-t61.log)"
    tail -1 /tmp/deploy-all-t61.log | grep -q "59 passed, 0 failed" || fail "batch-resume tests: unexpected result"
    python3 "$REPO/pending-deploy/batch-worker-safety/test_batch_worker_safety.py" > /tmp/deploy-all-t62.log 2>&1 \
        || fail "batch-worker-safety tests failed (see /tmp/deploy-all-t62.log)"
    tail -1 /tmp/deploy-all-t62.log | grep -q "32 passed, 0 failed" || fail "batch-worker-safety tests: unexpected result"
    python3 "$REPO/pending-deploy/webrtc-stranded-cleanup/test_webrtc_stranded_cleanup.py" > /tmp/deploy-all-t63.log 2>&1 \
        || fail "webrtc-stranded-cleanup tests failed (see /tmp/deploy-all-t63.log)"
    tail -1 /tmp/deploy-all-t63.log | grep -q "26 passed, 0 failed" || fail "webrtc-stranded-cleanup tests: unexpected result"
    bash "$REPO/pending-deploy/backup-visibility/test_backup_visibility.sh" > /tmp/deploy-all-t64a.log 2>&1 \
        || fail "backup-visibility shell tests failed (see /tmp/deploy-all-t64a.log)"
    tail -1 /tmp/deploy-all-t64a.log | grep -q "17 passed, 0 failed" || fail "backup-visibility shell tests: unexpected result"
    python3 "$REPO/pending-deploy/backup-visibility/test_backup_visibility.py" > /tmp/deploy-all-t64b.log 2>&1 \
        || fail "backup-visibility python tests failed (see /tmp/deploy-all-t64b.log)"
    tail -1 /tmp/deploy-all-t64b.log | grep -q "33 passed, 0 failed" || fail "backup-visibility python tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-session-guard/test_wing_session_guard.py" > /tmp/deploy-all-t65.log 2>&1 \
        || fail "wing-session-guard tests failed (see /tmp/deploy-all-t65.log)"
    tail -1 /tmp/deploy-all-t65.log | grep -q "49 passed, 0 failed" || fail "wing-session-guard tests: unexpected result"
    bash "$REPO/pending-deploy/loop-device-guard/test_loop_device_guard.sh" > /tmp/deploy-all-t66a.log 2>&1 \
        || fail "loop-device-guard shell tests failed (see /tmp/deploy-all-t66a.log)"
    tail -1 /tmp/deploy-all-t66a.log | grep -q "30 passed, 0 failed" || fail "loop-device-guard shell tests: unexpected result"
    python3 "$REPO/pending-deploy/loop-device-guard/test_loop_device_guard.py" > /tmp/deploy-all-t66b.log 2>&1 \
        || fail "loop-device-guard python tests failed (see /tmp/deploy-all-t66b.log)"
    tail -1 /tmp/deploy-all-t66b.log | grep -q "25 passed, 0 failed" || fail "loop-device-guard python tests: unexpected result"
    bash "$REPO/pending-deploy/wing-loop-devices/test_wing_loop_devices.sh" > /tmp/deploy-all-t67a.log 2>&1 \
        || fail "wing-loop-devices shell tests failed (see /tmp/deploy-all-t67a.log)"
    tail -1 /tmp/deploy-all-t67a.log | grep -q "16 passed, 0 failed" || fail "wing-loop-devices shell tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-loop-devices/test_wing_loop_devices.py" > /tmp/deploy-all-t67b.log 2>&1 \
        || fail "wing-loop-devices python tests failed (see /tmp/deploy-all-t67b.log)"
    tail -1 /tmp/deploy-all-t67b.log | grep -q "49 passed, 0 failed" || fail "wing-loop-devices python tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-mem-guard/test_wing_mem_guard.py" > /tmp/deploy-all-t68.log 2>&1 \
        || fail "wing-mem-guard tests failed (see /tmp/deploy-all-t68.log)"
    tail -1 /tmp/deploy-all-t68.log | grep -q "36 passed, 0 failed" || fail "wing-mem-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-stuck-boot/test_wing_stuck_boot.py" > /tmp/deploy-all-t69.log 2>&1 \
        || fail "wing-stuck-boot tests failed (see /tmp/deploy-all-t69.log)"
    tail -1 /tmp/deploy-all-t69.log | grep -q "26 passed, 0 failed" || fail "wing-stuck-boot tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-keepalive-mirror/test_wing_keepalive_mirror.py" > /tmp/deploy-all-t70.log 2>&1 \
        || fail "wing-keepalive-mirror tests failed (see /tmp/deploy-all-t70.log)"
    tail -1 /tmp/deploy-all-t70.log | grep -q "35 passed, 0 failed" || fail "wing-keepalive-mirror tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-binderfs-selfheal/test_wing_binderfs_selfheal.py" > /tmp/deploy-all-t71.log 2>&1 \
        || fail "wing-binderfs-selfheal tests failed (see /tmp/deploy-all-t71.log)"
    tail -1 /tmp/deploy-all-t71.log | grep -q "41 passed, 0 failed" || fail "wing-binderfs-selfheal tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-agent-selfcheck/test_wing_agent_selfcheck.py" > /tmp/deploy-all-t72.log 2>&1 \
        || fail "wing-agent-selfcheck tests failed (see /tmp/deploy-all-t72.log)"
    tail -1 /tmp/deploy-all-t72.log | grep -q "71 passed, 0 failed" || fail "wing-agent-selfcheck tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-create-preflight/test_wing_create_preflight.py" > /tmp/deploy-all-t73.log 2>&1 \
        || fail "wing-create-preflight tests failed (see /tmp/deploy-all-t73.log)"
    tail -1 /tmp/deploy-all-t73.log | grep -q "35 passed, 0 failed" || fail "wing-create-preflight tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-frame-cache/test_remote_frame_cache.py" > /tmp/deploy-all-t74.log 2>&1 \
        || fail "remote-frame-cache tests failed (see /tmp/deploy-all-t74.log)"
    tail -1 /tmp/deploy-all-t74.log | grep -q "31 passed, 0 failed" || fail "remote-frame-cache tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-auth-ratelimit/test_wing_auth_ratelimit.py" > /tmp/deploy-all-t75.log 2>&1 \
        || fail "wing-auth-ratelimit tests failed (see /tmp/deploy-all-t75.log)"
    tail -1 /tmp/deploy-all-t75.log | grep -q "48 passed, 0 failed" || fail "wing-auth-ratelimit tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-status-cache/test_wing_status_cache.py" > /tmp/deploy-all-t76.log 2>&1 \
        || fail "wing-status-cache tests failed (see /tmp/deploy-all-t76.log)"
    tail -1 /tmp/deploy-all-t76.log | grep -q "PASS=28 FAIL=0" || fail "wing-status-cache tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-headroom/test_remote_headroom.py" > /tmp/deploy-all-t77.log 2>&1 \
        || fail "remote-headroom tests failed (see /tmp/deploy-all-t77.log)"
    tail -1 /tmp/deploy-all-t77.log | grep -q "40 passed, 0 failed" || fail "remote-headroom tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-agent-update/test_wing_agent_update.py" > /tmp/deploy-all-t78.log 2>&1 \
        || fail "wing-agent-update tests failed (see /tmp/deploy-all-t78.log)"
    tail -1 /tmp/deploy-all-t78.log | grep -q "PASS=43 FAIL=0" || fail "wing-agent-update tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-degraded/test_remote_degraded.py" > /tmp/deploy-all-t79.log 2>&1 \
        || fail "remote-degraded tests failed (see /tmp/deploy-all-t79.log)"
    tail -1 /tmp/deploy-all-t79.log | grep -q "46 passed, 0 failed" || fail "remote-degraded tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-selfwatch-lock/test_wing_selfwatch_lock.py" > /tmp/deploy-all-t80.log 2>&1 \
        || fail "wing-selfwatch-lock tests failed (see /tmp/deploy-all-t80.log)"
    tail -1 /tmp/deploy-all-t80.log | grep -q "53 passed, 0 failed" || fail "wing-selfwatch-lock tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-agent-selfupdate/test_wing_agent_selfupdate.py" > /tmp/deploy-all-t81.log 2>&1 \
        || fail "wing-agent-selfupdate tests failed (see /tmp/deploy-all-t81.log)"
    tail -1 /tmp/deploy-all-t81.log | grep -q "63 passed, 0 failed" || fail "wing-agent-selfupdate tests: unexpected result"
    python3 "$REPO/pending-deploy/route-name-gate/test_route_name_gate.py" > /tmp/deploy-all-t82.log 2>&1 \
        || fail "route-name-gate tests failed (see /tmp/deploy-all-t82.log)"
    tail -1 /tmp/deploy-all-t82.log | grep -q "35 passed, 0 failed" || fail "route-name-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-route-name-gate/test_wing_route_name_gate.py" > /tmp/deploy-all-t83.log 2>&1 \
        || fail "wing-route-name-gate tests failed (see /tmp/deploy-all-t83.log)"
    tail -1 /tmp/deploy-all-t83.log | grep -q "33 passed, 0 failed" || fail "wing-route-name-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/node-caps-visibility/test_node_caps_visibility.py" > /tmp/deploy-all-t84.log 2>&1 \
        || fail "node-caps-visibility tests failed (see /tmp/deploy-all-t84.log)"
    tail -1 /tmp/deploy-all-t84.log | grep -q "PASS=33 FAIL=0" || fail "node-caps-visibility tests: unexpected result"
    python3 "$REPO/pending-deploy/login-ratelimit-tunnel/test_login_ratelimit_tunnel.py" > /tmp/deploy-all-t85.log 2>&1 \
        || fail "login-ratelimit-tunnel tests failed (see /tmp/deploy-all-t85.log)"
    tail -1 /tmp/deploy-all-t85.log | grep -q "34 passed, 0 failed" || fail "login-ratelimit-tunnel tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-ratelimit-tunnel/test_wing_ratelimit_tunnel.py" > /tmp/deploy-all-t86.log 2>&1 \
        || fail "wing-ratelimit-tunnel tests failed (see /tmp/deploy-all-t86.log)"
    tail -1 /tmp/deploy-all-t86.log | grep -q "PASS=38 FAIL=0"
    bash "$REPO/pending-deploy/boot-complete/test_boot_complete.sh" > /tmp/deploy-all-t87.log 2>&1 \
        || fail "boot-complete tests failed (see /tmp/deploy-all-t87.log)"
    tail -1 /tmp/deploy-all-t87.log | grep -q "63 passed, 0 failed" || fail "boot-complete tests: unexpected result" || fail "wing-ratelimit-tunnel tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-missing-script-guard/test_wing_missing_script_guard.py" > /tmp/deploy-all-t88.log 2>&1 \
        || fail "wing-missing-script-guard tests failed (see /tmp/deploy-all-t88.log)"
    tail -1 /tmp/deploy-all-t88.log | grep -q "PASS=35 FAIL=0" || fail "wing-missing-script-guard tests: unexpected result"
    bash "$REPO/pending-deploy/wing-installer-agent-check/test_wing_installer_agent_check.sh" > /tmp/deploy-all-t89.log 2>&1 \
        || fail "wing-installer-agent-check tests failed (see /tmp/deploy-all-t89.log)"
    tail -1 /tmp/deploy-all-t89.log | grep -q "20 passed, 0 failed" || fail "wing-installer-agent-check tests: unexpected result"
    bash "$REPO/pending-deploy/wing-installer-staging/test_wing_installer_staging.sh" > /tmp/deploy-all-t90.log 2>&1 \
        || fail "wing-installer-staging tests failed (see /tmp/deploy-all-t90.log)"
    tail -1 /tmp/deploy-all-t90.log | grep -q "PASS=32 FAIL=0" || fail "wing-installer-staging tests: unexpected result"
    python3 "$REPO/pending-deploy/login-username-escape/test_login_username_escape.py" > /tmp/deploy-all-t91.log 2>&1 \
        || fail "login-username-escape tests failed (see /tmp/deploy-all-t91.log)"
    tail -1 /tmp/deploy-all-t91.log | grep -q "PASS=51 FAIL=0" || fail "login-username-escape tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-timeout/test_watchdog_timeout.sh" > /tmp/deploy-all-t92.log 2>&1 \
        || fail "watchdog-timeout tests failed (see /tmp/deploy-all-t92.log)"
    tail -1 /tmp/deploy-all-t92.log | grep -q "PASS=50 FAIL=0" || fail "watchdog-timeout tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-update-rollback/test_wing_update_rollback.py" > /tmp/deploy-all-t93.log 2>&1 \
        || fail "wing-update-rollback tests failed (see /tmp/deploy-all-t93.log)"
    tail -1 /tmp/deploy-all-t93.log | grep -q "PASS=53 FAIL=0" || fail "wing-update-rollback tests: unexpected result"
    bash "$REPO/pending-deploy/install-baseline/test_install_baseline.sh" > /tmp/deploy-all-t94.log 2>&1 \
        || fail "install-baseline tests failed (see /tmp/deploy-all-t94.log)"
    tail -1 /tmp/deploy-all-t94.log | grep -q "27 passed, 0 failed" || fail "install-baseline tests: unexpected result"
    bash "$REPO/pending-deploy/keepalive-probe-timeout/test_keepalive_probe_timeout.sh" > /tmp/deploy-all-t95.log 2>&1 \
        || fail "keepalive-probe-timeout tests failed (see /tmp/deploy-all-t95.log)"
    tail -1 /tmp/deploy-all-t95.log | grep -q "PASS=32 FAIL=0" || fail "keepalive-probe-timeout tests: unexpected result"
    bash "$REPO/pending-deploy/watcher-probe-timeout/test_watcher_probe_timeout.sh" > /tmp/deploy-all-t96.log 2>&1 \
        || fail "watcher-probe-timeout tests failed (see /tmp/deploy-all-t96.log)"
    tail -1 /tmp/deploy-all-t96.log | grep -q "26 passed, 0 failed" || fail "watcher-probe-timeout tests: unexpected result"
    python3 "$REPO/pending-deploy/nodes-conn-err/test_nodes_conn_err.py" > /tmp/deploy-all-t97.log 2>&1 \
        || fail "nodes-conn-err tests failed (see /tmp/deploy-all-t97.log)"
    tail -1 /tmp/deploy-all-t97.log | grep -q "25 passed, 0 failed" || fail "nodes-conn-err tests: unexpected result"
    bash "$REPO/pending-deploy/boot-install-timeout/test_boot_install_timeout.sh" > /tmp/deploy-all-t98.log 2>&1 \
        || fail "boot-install-timeout tests failed (see /tmp/deploy-all-t98.log)"
    tail -1 /tmp/deploy-all-t98.log | grep -q "38 passed, 0 failed" || fail "boot-install-timeout tests: unexpected result"
    python3 "$REPO/pending-deploy/bulk-accounts-commas/test_bulk_accounts_commas.py" > /tmp/deploy-all-t99.log 2>&1 \
        || fail "bulk-accounts-commas tests failed (see /tmp/deploy-all-t99.log)"
    tail -1 /tmp/deploy-all-t99.log | grep -q "24 passed, 0 failed" || fail "bulk-accounts-commas tests: unexpected result"
    python3 "$REPO/pending-deploy/instance-resize/test_instance_resize.py" > /tmp/deploy-all-t100.log 2>&1 \
        || fail "instance-resize tests failed (see /tmp/deploy-all-t100.log)"
    tail -1 /tmp/deploy-all-t100.log | grep -q "PASS=77 FAIL=0" || fail "instance-resize tests: unexpected result"
    bash "$REPO/pending-deploy/boot-recovery/test_boot_recovery.sh" > /tmp/deploy-all-t101.log 2>&1 \
        || fail "boot-recovery tests failed (see /tmp/deploy-all-t101.log)"
    tail -1 /tmp/deploy-all-t101.log | grep -q "50 passed, 0 failed" || fail "boot-recovery tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-boot-recovery/test_wing_boot_recovery.py" > /tmp/deploy-all-t102.log 2>&1 \
        || fail "wing-boot-recovery tests failed (see /tmp/deploy-all-t102.log)"
    tail -1 /tmp/deploy-all-t102.log | grep -q "PASS=85 FAIL=0" || fail "wing-boot-recovery tests: unexpected result"
    python3 "$REPO/pending-deploy/binder-safe-start/test_binder_safe_start.py" > /tmp/deploy-all-t103.log 2>&1 \
        || fail "binder-safe-start tests failed (see /tmp/deploy-all-t103.log)"
    tail -1 /tmp/deploy-all-t103.log | grep -q "PASS=25 FAIL=0" || fail "binder-safe-start tests: unexpected result"
    bash "$REPO/pending-deploy/push-timeout/test_push_timeout.sh" > /tmp/deploy-all-t104.log 2>&1 \
        || fail "push-timeout tests failed (see /tmp/deploy-all-t104.log)"
    tail -1 /tmp/deploy-all-t104.log | grep -q "66 passed, 0 failed" || fail "push-timeout tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-status-invalidate/test_remote_status_invalidate.py" > /tmp/deploy-all-t105.log 2>&1 \
        || fail "remote-status-invalidate tests failed (see /tmp/deploy-all-t105.log)"
    tail -1 /tmp/deploy-all-t105.log | grep -q "PASS=28 FAIL=0" || fail "remote-status-invalidate tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-start-route-dedup/test_wing_start_route_dedup.py" > /tmp/deploy-all-t106.log 2>&1 \
        || fail "wing-start-route-dedup tests failed (see /tmp/deploy-all-t106.log)"
    tail -1 /tmp/deploy-all-t106.log | grep -q "9 passed, 0 failed" || fail "wing-start-route-dedup tests: unexpected result"
    python3 "$REPO/pending-deploy/fleet-cpu-headroom/test_fleet_cpu_headroom.py" > /tmp/deploy-all-t107.log 2>&1 \
        || fail "fleet-cpu-headroom tests failed (see /tmp/deploy-all-t107.log)"
    tail -1 /tmp/deploy-all-t107.log | grep -q "PASS=19 FAIL=0" || fail "fleet-cpu-headroom tests: unexpected result"
    bash "$REPO/pending-deploy/roblox-install-guard/test_roblox_install_guard.sh" > /tmp/deploy-all-t108.log 2>&1 \
        || fail "roblox-install-guard tests failed (see /tmp/deploy-all-t108.log)"
    tail -1 /tmp/deploy-all-t108.log | grep -q "PASS=32 FAIL=0" || fail "roblox-install-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-install-guard/test_wing_install_guard.py" > /tmp/deploy-all-t109.log 2>&1 \
        || fail "wing-install-guard tests failed (see /tmp/deploy-all-t109.log)"
    tail -1 /tmp/deploy-all-t109.log | grep -q "PASS=34 FAIL=0" || fail "wing-install-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/resize-cache-invalidate/test_resize_cache_invalidate.py" > /tmp/deploy-all-t110.log 2>&1 \
        || fail "resize-cache-invalidate tests failed (see /tmp/deploy-all-t110.log)"
    tail -1 /tmp/deploy-all-t110.log | grep -q "PASS=18 FAIL=0" || fail "resize-cache-invalidate tests: unexpected result"
    python3 "$REPO/pending-deploy/agent-body-cap/test_agent_body_cap.py" > /tmp/deploy-all-t111.log 2>&1 \
        || fail "agent-body-cap tests failed (see /tmp/deploy-all-t111.log)"
    tail -1 /tmp/deploy-all-t111.log | grep -q "PASS=20 FAIL=0" || fail "agent-body-cap tests: unexpected result"
    python3 "$REPO/pending-deploy/wing-update-path/test_wing_update_path.py" > /tmp/deploy-all-t112.log 2>&1 \
        || fail "wing-update-path tests failed (see /tmp/deploy-all-t112.log)"
    tail -1 /tmp/deploy-all-t112.log | grep -q "PASS=20 FAIL=0" || fail "wing-update-path tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-liveness/test_watchdog_liveness.sh" > /tmp/deploy-all-t113.log 2>&1 \
        || fail "watchdog-liveness tests failed (see /tmp/deploy-all-t113.log)"
    tail -1 /tmp/deploy-all-t113.log | grep -q "PASS=11 FAIL=0" || fail "watchdog-liveness tests: unexpected result"
    python3 "$REPO/pending-deploy/webrtc-handshake-cleanup/test_webrtc_handshake_cleanup.py" > /tmp/deploy-all-t114.log 2>&1 \
        || fail "webrtc-handshake-cleanup tests failed (see /tmp/deploy-all-t114.log)"
    tail -1 /tmp/deploy-all-t114.log | grep -q "34 passed, 0 failed" || fail "webrtc-handshake-cleanup tests: unexpected result"
    python3 "$REPO/pending-deploy/create-ip-gate/test_create_ip_gate.py" > /tmp/deploy-all-t115.log 2>&1 \
        || fail "create-ip-gate tests failed (see /tmp/deploy-all-t115.log)"
    tail -1 /tmp/deploy-all-t115.log | grep -q "PASS=98 FAIL=0" || fail "create-ip-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-input-b64/test_remote_input_b64.py" > /tmp/deploy-all-t116.log 2>&1 \
        || fail "remote-input-b64 tests failed (see /tmp/deploy-all-t116.log)"
    tail -1 /tmp/deploy-all-t116.log | grep -q "44 passed, 0 failed" || fail "remote-input-b64 tests: unexpected result"
    python3 "$REPO/pending-deploy/token-atomic-write/test_token_atomic_write.py" > /tmp/deploy-all-t117.log 2>&1 \
        || fail "token-atomic-write tests failed (see /tmp/deploy-all-t117.log)"
    tail -1 /tmp/deploy-all-t117.log | grep -q "PASS=25 FAIL=0" || fail "token-atomic-write tests: unexpected result"
    python3 "$REPO/pending-deploy/login-pwd-cleanup/test_login_pwd_cleanup.py" > /tmp/deploy-all-t118.log 2>&1 \
        || fail "login-pwd-cleanup tests failed (see /tmp/deploy-all-t118.log)"
    tail -1 /tmp/deploy-all-t118.log | grep -q "PASS=32 FAIL=0" || fail "login-pwd-cleanup tests: unexpected result"
    python3 "$REPO/pending-deploy/read-body-missing/test_read_body_missing.py" > /tmp/deploy-all-t119.log 2>&1 \
        || fail "read-body-missing tests failed (see /tmp/deploy-all-t119.log)"
    tail -1 /tmp/deploy-all-t119.log | grep -q "PASS=20 FAIL=0" || fail "read-body-missing tests: unexpected result"
    python3 "$REPO/pending-deploy/agent-ip-gate/test_agent_ip_gate.py" > /tmp/deploy-all-t120.log 2>&1 \
        || fail "agent-ip-gate tests failed (see /tmp/deploy-all-t120.log)"
    tail -1 /tmp/deploy-all-t120.log | grep -q "PASS=66 FAIL=0" || fail "agent-ip-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-launch-fifo/test_remote_launch_fifo.py" > /tmp/deploy-all-t121.log 2>&1 \
        || fail "remote-launch-fifo tests failed (see /tmp/deploy-all-t121.log)"
    tail -1 /tmp/deploy-all-t121.log | grep -q "PASS=19 FAIL=0" || fail "remote-launch-fifo tests: unexpected result"
    python3 "$REPO/pending-deploy/auto-login-success/test_auto_login_success.py" > /tmp/deploy-all-t122.log 2>&1 \
        || fail "auto-login-success tests failed (see /tmp/deploy-all-t122.log)"
    tail -1 /tmp/deploy-all-t122.log | grep -q "PASS=37 FAIL=0" || fail "auto-login-success tests: unexpected result"
    python3 "$REPO/pending-deploy/login-script-cleanup/test_login_script_cleanup.py" > /tmp/deploy-all-t123.log 2>&1 \
        || fail "login-script-cleanup tests failed (see /tmp/deploy-all-t123.log)"
    tail -1 /tmp/deploy-all-t123.log | grep -q "PASS=25 FAIL=0" || fail "login-script-cleanup tests: unexpected result"
    python3 "$REPO/pending-deploy/waydroid-exec-cleanup/test_waydroid_exec_cleanup.py" > /tmp/deploy-all-t124.log 2>&1 \
        || fail "waydroid-exec-cleanup tests failed (see /tmp/deploy-all-t124.log)"
    tail -1 /tmp/deploy-all-t124.log | grep -q "PASS=33 FAIL=0" || fail "waydroid-exec-cleanup tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-b64-sweep/test_remote_b64_sweep.py" > /tmp/deploy-all-t125.log 2>&1 \
        || fail "remote-b64-sweep tests failed (see /tmp/deploy-all-t125.log)"
    tail -1 /tmp/deploy-all-t125.log | grep -q "PASS=42 FAIL=0" || fail "remote-b64-sweep tests: unexpected result"
    python3 "$REPO/pending-deploy/state-tmp-sweep/test_state_tmp_sweep.py" > /tmp/deploy-all-t126.log 2>&1 \
        || fail "state-tmp-sweep tests failed (see /tmp/deploy-all-t126.log)"
    tail -1 /tmp/deploy-all-t126.log | grep -q "PASS=30 FAIL=0" || fail "state-tmp-sweep tests: unexpected result"
    python3 "$REPO/pending-deploy/providers-600/test_providers_600.py" > /tmp/deploy-all-t127.log 2>&1 \
        || fail "providers-600 tests failed (see /tmp/deploy-all-t127.log)"
    tail -1 /tmp/deploy-all-t127.log | grep -q "PASS=18 FAIL=0" || fail "providers-600 tests: unexpected result"
    python3 "$REPO/pending-deploy/state-600-complete/test_state_600_complete.py" > /tmp/deploy-all-t128.log 2>&1 \
        || fail "state-600-complete tests failed (see /tmp/deploy-all-t128.log)"
    tail -1 /tmp/deploy-all-t128.log | grep -q "PASS=33 FAIL=0" || fail "state-600-complete tests: unexpected result"
    python3 "$REPO/pending-deploy/login-script-600/test_login_script_600.py" > /tmp/deploy-all-t129.log 2>&1 \
        || fail "login-script-600 tests failed (see /tmp/deploy-all-t129.log)"
    tail -1 /tmp/deploy-all-t129.log | grep -q "PASS=33 FAIL=0" || fail "login-script-600 tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-daemon-guard/test_watchdog_v15.sh" > /tmp/deploy-all-t130.log 2>&1 \
        || fail "watchdog-daemon-guard tests failed (see /tmp/deploy-all-t130.log)"
    tail -1 /tmp/deploy-all-t130.log | grep -q "PASS=48 FAIL=0" || fail "watchdog-daemon-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/node-daemon-guard/test_node_daemon_guard.py" > /tmp/deploy-all-t131.log 2>&1 \
        || fail "node-daemon-guard tests failed (see /tmp/deploy-all-t131.log)"
    tail -1 /tmp/deploy-all-t131.log | grep -q "PASS=55 FAIL=0" || fail "node-daemon-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/global-health/test_global_health.py" > /tmp/deploy-all-t132.log 2>&1 \
        || fail "global-health tests failed (see /tmp/deploy-all-t132.log)"
    tail -1 /tmp/deploy-all-t132.log | grep -q "PASS=44 FAIL=0" || fail "global-health tests: unexpected result"
    python3 "$REPO/pending-deploy/nodes-attention-badge/test_nodes_attention_badge.py" > /tmp/deploy-all-t133.log 2>&1 \
        || fail "nodes-attention-badge tests failed (see /tmp/deploy-all-t133.log)"
    tail -1 /tmp/deploy-all-t133.log | grep -q "PASS=58 FAIL=0" || fail "nodes-attention-badge tests: unexpected result"
    python3 "$REPO/pending-deploy/delete-state-sweep/test_delete_state_sweep.py" > /tmp/deploy-all-t134.log 2>&1 \
        || fail "delete-state-sweep tests failed (see /tmp/deploy-all-t134.log)"
    tail -1 /tmp/deploy-all-t134.log | grep -q "PASS=45 FAIL=0" || fail "delete-state-sweep tests: unexpected result"
    python3 "$REPO/pending-deploy/node-offline-badge/test_node_offline_badge.py" > /tmp/deploy-all-t135.log 2>&1 \
        || fail "node-offline-badge tests failed (see /tmp/deploy-all-t135.log)"
    tail -1 /tmp/deploy-all-t135.log | grep -q "PASS=48 FAIL=0" || fail "node-offline-badge tests: unexpected result"
    python3 "$REPO/pending-deploy/fleet-resources-remote/test_fleet_resources_remote.py" > /tmp/deploy-all-t136.log 2>&1 \
        || fail "fleet-resources-remote tests failed (see /tmp/deploy-all-t136.log)"
    tail -1 /tmp/deploy-all-t136.log | grep -q "PASS=54 FAIL=0" || fail "fleet-resources-remote tests: unexpected result"
    python3 "$REPO/pending-deploy/boot-recovery-badge/test_boot_recovery_badge.py" > /tmp/deploy-all-t137.log 2>&1 \
        || fail "boot-recovery-badge tests failed (see /tmp/deploy-all-t137.log)"
    tail -1 /tmp/deploy-all-t137.log | grep -q "PASS=42 FAIL=0" || fail "boot-recovery-badge tests: unexpected result"
    python3 "$REPO/pending-deploy/watchdog-heartbeat-banner/test_watchdog_heartbeat_banner.py" > /tmp/deploy-all-t138.log 2>&1 \
        || fail "watchdog-heartbeat-banner tests failed (see /tmp/deploy-all-t138.log)"
    tail -1 /tmp/deploy-all-t138.log | grep -q "PASS=18 FAIL=0" || fail "watchdog-heartbeat-banner tests: unexpected result"
    python3 "$REPO/pending-deploy/node-fetch-badge/test_node_fetch_badge.py" > /tmp/deploy-all-t139.log 2>&1 \
        || fail "node-fetch-badge tests failed (see /tmp/deploy-all-t139.log)"
    tail -1 /tmp/deploy-all-t139.log | grep -q "PASS=62 FAIL=0" || fail "node-fetch-badge tests: unexpected result"
    python3 "$REPO/pending-deploy/mjpeg-terminal-guard/test_mjpeg_terminal_guard.py" > /tmp/deploy-all-t140.log 2>&1 \
        || fail "mjpeg-terminal-guard tests failed (see /tmp/deploy-all-t140.log)"
    tail -1 /tmp/deploy-all-t140.log | grep -q "PASS=35 FAIL=0" || fail "mjpeg-terminal-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/single-restart-route/test_single_restart_route.py" > /tmp/deploy-all-t141.log 2>&1 \
        || fail "single-restart-route tests failed (see /tmp/deploy-all-t141.log)"
    tail -1 /tmp/deploy-all-t141.log | grep -q "PASS=30 FAIL=0" || fail "single-restart-route tests: unexpected result"
    python3 "$REPO/pending-deploy/stale-frame-gate/test_stale_frame_gate.py" > /tmp/deploy-all-t142.log 2>&1 \
        || fail "stale-frame-gate tests failed (see /tmp/deploy-all-t142.log)"
    tail -1 /tmp/deploy-all-t142.log | grep -q "PASS=35 FAIL=0" || fail "stale-frame-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/launch-focus-gate/test_launch_focus_gate.py" > /tmp/deploy-all-t143.log 2>&1 \
        || fail "launch-focus-gate tests failed (see /tmp/deploy-all-t143.log)"
    tail -1 /tmp/deploy-all-t143.log | grep -q "PASS=35 FAIL=0" || fail "launch-focus-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-fetch-exception-cache/test_remote_fetch_exception_cache.py" > /tmp/deploy-all-t144.log 2>&1 \
        || fail "remote-fetch-exception-cache tests failed (see /tmp/deploy-all-t144.log)"
    tail -1 /tmp/deploy-all-t144.log | grep -q "PASS=27 FAIL=0" || fail "remote-fetch-exception-cache tests: unexpected result"
    python3 "$REPO/pending-deploy/ws-reader-timeout/test_ws_reader_timeout.py" > /tmp/deploy-all-t145.log 2>&1 \
        || fail "ws-reader-timeout tests failed (see /tmp/deploy-all-t145.log)"
    tail -1 /tmp/deploy-all-t145.log | grep -q "PASS=28 FAIL=0" || fail "ws-reader-timeout tests: unexpected result"
    python3 "$REPO/pending-deploy/h264-init-pin/test_h264_init_pin.py" > /tmp/deploy-all-t146.log 2>&1 \
        || fail "h264-init-pin tests failed (see /tmp/deploy-all-t146.log)"
    tail -1 /tmp/deploy-all-t146.log | grep -q "PASS=23 FAIL=0" || fail "h264-init-pin tests: unexpected result"
    python3 "$REPO/pending-deploy/missing-frame-guard/test_missing_frame_guard.py" > /tmp/deploy-all-t147.log 2>&1 \
        || fail "missing-frame-guard tests failed (see /tmp/deploy-all-t147.log)"
    tail -1 /tmp/deploy-all-t147.log | grep -q "PASS=27 FAIL=0" || fail "missing-frame-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/input-coord-validation/test_input_coord_validation.py" > /tmp/deploy-all-t148.log 2>&1 \
        || fail "input-coord-validation tests failed (see /tmp/deploy-all-t148.log)"
    tail -1 /tmp/deploy-all-t148.log | grep -q "PASS=38 FAIL=0" || fail "input-coord-validation tests: unexpected result"
    python3 "$REPO/pending-deploy/numeric-overflow-guard/test_numeric_overflow_guard.py" > /tmp/deploy-all-t149.log 2>&1 \
        || fail "numeric-overflow-guard tests failed (see /tmp/deploy-all-t149.log)"
    tail -1 /tmp/deploy-all-t149.log | grep -q "PASS=43 FAIL=0" || fail "numeric-overflow-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/non-dict-json-guard/test_non_dict_json_guard.py" > /tmp/deploy-all-t150.log 2>&1 \
        || fail "non-dict-json-guard tests failed (see /tmp/deploy-all-t150.log)"
    tail -1 /tmp/deploy-all-t150.log | grep -q "PASS=39 FAIL=0" || fail "non-dict-json-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/request-type-guard/test_request_type_guard.py" > /tmp/deploy-all-t151.log 2>&1 \
        || fail "request-type-guard tests failed (see /tmp/deploy-all-t151.log)"
    tail -1 /tmp/deploy-all-t151.log | grep -q "PASS=32 FAIL=0" || fail "request-type-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/state-shape-guard/test_state_shape_guard.py" > /tmp/deploy-all-t152.log 2>&1 \
        || fail "state-shape-guard tests failed (see /tmp/deploy-all-t152.log)"
    tail -1 /tmp/deploy-all-t152.log | grep -q "PASS=33 FAIL=0" || fail "state-shape-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/state-load-robustness/test_state_load_robustness.py" > /tmp/deploy-all-t153.log 2>&1 \
        || fail "state-load-robustness tests failed (see /tmp/deploy-all-t153.log)"
    tail -1 /tmp/deploy-all-t153.log | grep -q "PASS=40 FAIL=0" || fail "state-load-robustness tests: unexpected result"
    python3 "$REPO/pending-deploy/provider-record-guard/test_provider_record_guard.py" > /tmp/deploy-all-t154.log 2>&1 \
        || fail "provider-record-guard tests failed (see /tmp/deploy-all-t154.log)"
    tail -1 /tmp/deploy-all-t154.log | grep -q "PASS=60 FAIL=0" || fail "provider-record-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/instance-record-guard/test_instance_record_guard.py" > /tmp/deploy-all-t155.log 2>&1 \
        || fail "instance-record-guard tests failed (see /tmp/deploy-all-t155.log)"
    tail -1 /tmp/deploy-all-t155.log | grep -q "PASS=49 FAIL=0" || fail "instance-record-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/account-record-guard/test_account_record_guard.py" > /tmp/deploy-all-t156.log 2>&1 \
        || fail "account-record-guard tests failed (see /tmp/deploy-all-t156.log)"
    tail -1 /tmp/deploy-all-t156.log | grep -q "PASS=34 FAIL=0" || fail "account-record-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/route-auth-census/test_route_auth_census.py" > /tmp/deploy-all-t157.log 2>&1 \
        || fail "route-auth-census tests failed (see /tmp/deploy-all-t157.log)"
    tail -1 /tmp/deploy-all-t157.log | grep -q "PASS=78 FAIL=0" || fail "route-auth-census tests: unexpected result"
    python3 "$REPO/pending-deploy/static-path-guard/test_static_path_guard.py" > /tmp/deploy-all-t158.log 2>&1 \
        || fail "static-path-guard tests failed (see /tmp/deploy-all-t158.log)"
    tail -1 /tmp/deploy-all-t158.log | grep -q "PASS=20 FAIL=0" || fail "static-path-guard tests: unexpected result"
    python3 "$REPO/pending-deploy/dashboard-html-escape/test_dashboard_html_escape.py" > /tmp/deploy-all-t159.log 2>&1 \
        || fail "dashboard-html-escape tests failed (see /tmp/deploy-all-t159.log)"
    tail -1 /tmp/deploy-all-t159.log | grep -q "PASS=12 FAIL=0" || fail "dashboard-html-escape tests: unexpected result"
    bash "$REPO/pending-deploy/watchdog-name-gate/test_watchdog_name_gate.sh" > /tmp/deploy-all-t160.log 2>&1 \
        || fail "watchdog-name-gate tests failed (see /tmp/deploy-all-t160.log)"
    tail -1 /tmp/deploy-all-t160.log | grep -q "PASS=21 FAIL=0" || fail "watchdog-name-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/login-name-gate/test_login_name_gate.py" > /tmp/deploy-all-t161.log 2>&1 \
        || fail "login-name-gate tests failed (see /tmp/deploy-all-t161.log)"
    tail -1 /tmp/deploy-all-t161.log | grep -q "PASS=34 FAIL=0" || fail "login-name-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/create-sink-gate/test_create_sink_gate.py" > /tmp/deploy-all-t162.log 2>&1 \
        || fail "create-sink-gate tests failed (see /tmp/deploy-all-t162.log)"
    tail -1 /tmp/deploy-all-t162.log | grep -q "PASS=47 FAIL=0" || fail "create-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/resize-sink-gate/test_resize_sink_gate.py" > /tmp/deploy-all-t163.log 2>&1 \
        || fail "resize-sink-gate tests failed (see /tmp/deploy-all-t163.log)"
    tail -1 /tmp/deploy-all-t163.log | grep -q "PASS=45 FAIL=0" || fail "resize-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/action-sink-gate/test_action_sink_gate.py" > /tmp/deploy-all-t164.log 2>&1 \
        || fail "action-sink-gate tests failed (see /tmp/deploy-all-t164.log)"
    tail -1 /tmp/deploy-all-t164.log | grep -q "PASS=53 FAIL=0" || fail "action-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/screenshot-path-gate/test_screenshot_path_gate.py" > /tmp/deploy-all-t165.log 2>&1 \
        || fail "screenshot-path-gate tests failed (see /tmp/deploy-all-t165.log)"
    tail -1 /tmp/deploy-all-t165.log | grep -q "PASS=29 FAIL=0" || fail "screenshot-path-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/input-sink-gate/test_input_sink_gate.py" > /tmp/deploy-all-t166.log 2>&1 \
        || fail "input-sink-gate tests failed (see /tmp/deploy-all-t166.log)"
    tail -1 /tmp/deploy-all-t166.log | grep -q "PASS=44 FAIL=0" || fail "input-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/autologin-probe-gate/test_autologin_probe_gate.py" > /tmp/deploy-all-t167.log 2>&1 \
        || fail "autologin-probe-gate tests failed (see /tmp/deploy-all-t167.log)"
    tail -1 /tmp/deploy-all-t167.log | grep -q "PASS=64 FAIL=0" || fail "autologin-probe-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-resize-sink-gate/test_remote_resize_sink_gate.py" > /tmp/deploy-all-t168.log 2>&1 \
        || fail "remote-resize-sink-gate tests failed (see /tmp/deploy-all-t168.log)"
    tail -1 /tmp/deploy-all-t168.log | grep -q "PASS=49 FAIL=0" || fail "remote-resize-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/remote-screenshot-sink-gate/test_remote_screenshot_sink_gate.py" > /tmp/deploy-all-t169.log 2>&1 \
        || fail "remote-screenshot-sink-gate tests failed (see /tmp/deploy-all-t169.log)"
    tail -1 /tmp/deploy-all-t169.log | grep -q "PASS=34 FAIL=0" || fail "remote-screenshot-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/agent-status-delete-sink-gate/test_agent_status_delete_sink_gate.py" > /tmp/deploy-all-t170.log 2>&1 \
        || fail "agent-status-delete-sink-gate tests failed (see /tmp/deploy-all-t170.log)"
    tail -1 /tmp/deploy-all-t170.log | grep -q "PASS=121 FAIL=0" || fail "agent-status-delete-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/agent-create-numerics-sink-gate/test_agent_create_numerics_sink_gate.py" > /tmp/deploy-all-t171.log 2>&1 \
        || fail "agent-create-numerics-sink-gate tests failed (see /tmp/deploy-all-t171.log)"
    tail -1 /tmp/deploy-all-t171.log | grep -q "PASS=89 FAIL=0" || fail "agent-create-numerics-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/agent-resize-numerics-sink-gate/test_agent_resize_numerics_sink_gate.py" > /tmp/deploy-all-t172.log 2>&1 \
        || fail "agent-resize-numerics-sink-gate tests failed (see /tmp/deploy-all-t172.log)"
    tail -1 /tmp/deploy-all-t172.log | grep -q "PASS=101 FAIL=0" || fail "agent-resize-numerics-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/agent-binder-count-sink-gate/test_agent_binder_count_sink_gate.py" > /tmp/deploy-all-t173.log 2>&1 \
        || fail "agent-binder-count-sink-gate tests failed (see /tmp/deploy-all-t173.log)"
    tail -1 /tmp/deploy-all-t173.log | grep -q "PASS=148 FAIL=0" || fail "agent-binder-count-sink-gate tests: unexpected result"
    python3 "$REPO/pending-deploy/agent-sw-numerics-sink-gate/test_agent_sw_numerics_sink_gate.py" > /tmp/deploy-all-t174.log 2>&1 \
        || fail "agent-sw-numerics-sink-gate tests failed (see /tmp/deploy-all-t174.log)"
    tail -1 /tmp/deploy-all-t174.log | grep -q "PASS=96 FAIL=0" || fail "agent-sw-numerics-sink-gate tests: unexpected result"
    log "tests green: js-gate 10/10, h264 59/59, remote-autologin 32/32, watchdog 26/26, watchdog-v5 27/27, watchdog-v6 40/40, selfwatch 33/33, instance-health 17/17, watchdog-v7 11/11, binderfs-selfheal 36/36, batch-ops 51/51, bulk-accounts 59/59, login-observability 44/44, batch-login 40/40, fleet-create 71/71, instance-resources 51/51, keepalive-v2 38/38, queue-audit 82/82, login-hardening 33/33, login-ratelimit 34/34, watchdog-session 34/34, remote-resources 43/43, log-trimmer 23/23, booted-watcher 23/23, roblox-drift-badge 30/30, srt-version-drift 38/38, instance-health-cache 37/37, stream-freshness 27/27, stream-frameless-guard 24/24, watchdog-events 37/37, live-verify 119/119, thread-supervisor 36/36, backup-cron-repoint 23/23, adaptive-screencap 48/48, node-disk-guard 54/54, watchdog-lock 22/22, watchdog-v12 34/34, batch-journal 46/46, backup-state 40/40, http-server-bounds 38/38, webrtc-stream-want 40/40, wing-failfast 43/43, mjpeg-dedup 30/30, h264-skip-static 29/29, wing-selfwatch-dashboard 72/72, webrtc-frameless-guard 32/32, post-deploy 84/84, waydroid-image-build 76/76, newbox-baseline 53/53, state-migrate 59/59, newbox-sync 45/45, android-optimizer 45/45, create-preflight 22/22, fetch-roblox-apks 29/29, conn-banner 29/29, binder-verify 26/26, cron-ensure 44/44, webrtc-loop-supervisor 39/39, agent-thread-supervisor 29/29, batch-resume 59/59, batch-worker-safety 32/32, webrtc-stranded-cleanup 26/26, backup-visibility 50/50, wing-session-guard 49/49, loop-device-guard 55/55, wing-loop-devices 65/65, wing-mem-guard 36/36, wing-stuck-boot 26/26, wing-keepalive-mirror 35/35, wing-binderfs-selfheal 41/41, wing-agent-selfcheck 71/71, wing-create-preflight 35/35, remote-frame-cache 31/31, wing-auth-ratelimit 48/48, wing-status-cache 28/28, remote-headroom 40/40, wing-agent-update 43/43, remote-degraded 46/46, wing-selfwatch-lock 53/53, wing-agent-selfupdate 63/63, route-name-gate 35/35, wing-route-name-gate 33/33, node-caps-visibility 33/33, login-ratelimit-tunnel 34/34, wing-ratelimit-tunnel 38/38, boot-complete 63/63, wing-missing-script-guard 35/35, wing-installer-agent-check 20/20, wing-installer-staging 32/32, login-username-escape 51/51, watchdog-timeout 50/50, wing-update-rollback 53/53, install-baseline 27/27, keepalive-probe-timeout 32/32, watcher-probe-timeout 26/26, nodes-conn-err 25/25, boot-install-timeout 38/38, bulk-accounts-commas 24/24, instance-resize 77/77, boot-recovery 50/50, wing-boot-recovery 85/85, binder-safe-start 25/25, push-timeout 66/66, remote-status-invalidate 28/28, wing-start-route-dedup 9/9, fleet-cpu-headroom 19/19, roblox-install-guard 32/32, wing-install-guard 34/34, resize-cache-invalidate 18/18, agent-body-cap 20/20, wing-update-path 20/20, watchdog-liveness 11/11, webrtc-handshake-cleanup 34/34, create-ip-gate 98/98, remote-input-b64 44/44, token-atomic-write 25/25, login-pwd-cleanup 32/32, read-body-missing 20/20, agent-ip-gate 66/66, remote-launch-fifo 19/19, auto-login-success 37/37, login-script-cleanup 25/25, waydroid-exec-cleanup 33/33, remote-b64-sweep 42/42, state-tmp-sweep 30/30, providers-600 18/18, state-600-complete 33/33, login-script-600 33/33, watchdog-daemon-guard 48/48, node-daemon-guard 55/55, global-health 44/44, nodes-attention-badge 58/58, delete-state-sweep 45/45, node-offline-badge 48/48, fleet-resources-remote 54/54, boot-recovery-badge 42/42, watchdog-heartbeat-banner 18/18, node-fetch-badge 62/62, mjpeg-terminal-guard 35/35, single-restart-route 30/30, stale-frame-gate 35/35, launch-focus-gate 35/35, remote-fetch-exception-cache 27/27, ws-reader-timeout 28/28, h264-init-pin 23/23, missing-frame-guard 27/27, input-coord-validation 38/38, numeric-overflow-guard 43/43, non-dict-json-guard 39/39, request-type-guard 32/32, state-shape-guard 33/33, state-load-robustness 40/40, provider-record-guard 60/60, instance-record-guard 49/49, account-record-guard 34/34, route-auth-census 78/78, static-path-guard 20/20, dashboard-html-escape 12/12, watchdog-name-gate 21/21, login-name-gate 34/34, create-sink-gate 47/47, resize-sink-gate 45/45, action-sink-gate 53/53, screenshot-path-gate 29/29, input-sink-gate 44/44, autologin-probe-gate 64/64, remote-resize-sink-gate 49/49, remote-screenshot-sink-gate 34/34, agent-status-delete-sink-gate 121/121, agent-create-numerics-sink-gate 89/89, agent-resize-numerics-sink-gate 101/101, agent-binder-count-sink-gate 148/148, agent-sw-numerics-sink-gate 96/96"

    log "preflight: syntax"
    bash "$REPO/pending-deploy/apply-all/check-js.sh" > /tmp/deploy-all-js.log 2>&1 \
        || fail "dashboard JS syntax gate failed (see /tmp/deploy-all-js.log)"
    python3 -m py_compile "$REPO/manager/app.py" "$REPO/manager/h264_stream.py" "$REPO/wing/agent.py" \
        || fail "py_compile failed"
    bash -n "$REPO/waydroid-image/df-watchdog.sh" || fail "bash -n df-watchdog.sh failed"
    bash -n "$REPO/waydroid-image/roblox-keepalive.sh" || fail "bash -n roblox-keepalive.sh failed"
    bash -n "$REPO/waydroid-image/booted-watcher.sh" || fail "bash -n booted-watcher.sh failed"
    bash -n "$REPO/waydroid-image/boot-complete.sh" || fail "bash -n boot-complete.sh failed"
    bash -n "$REPO/pending-deploy/backup-cron-repoint/repoint-backup-cron.sh" || fail "bash -n repoint-backup-cron.sh failed"
    bash -n "$REPO/waydroid-image/screencap-loop.sh" || fail "bash -n screencap-loop.sh failed"
    bash -n "$REPO/pending-deploy/adaptive-screencap/push-screencap.sh" || fail "bash -n push-screencap.sh failed"
    bash -n "$REPO/pending-deploy/keepalive-v2/push-keepalive.sh" || fail "bash -n push-keepalive.sh failed"
    bash -n "$REPO/pending-deploy/booted-watcher/push-booted-watcher.sh" || fail "bash -n push-booted-watcher.sh failed"
    bash -n "$REPO/waydroid-image/android-optimize.sh" || fail "bash -n android-optimize.sh failed"
    bash -n "$REPO/pending-deploy/android-optimizer/push-optimizer.sh" || fail "bash -n push-optimizer.sh failed"
    bash -n "$REPO/pending-deploy/post-deploy/finish-deploy.sh" || fail "bash -n finish-deploy.sh failed"
    bash -n "$REPO/pending-deploy/waydroid-image-build/extract-roblox-apks.sh" || fail "bash -n extract-roblox-apks.sh failed"
    bash -n "$REPO/pending-deploy/waydroid-image-build/vps-image-build.sh" || fail "bash -n vps-image-build.sh failed"
    bash -n "$REPO/pending-deploy/waydroid-image-build/fetch-roblox-apks.sh" || fail "bash -n fetch-roblox-apks.sh failed"
    bash -n "$REPO/pending-deploy/binder-verify/verify-binder.sh" || fail "bash -n verify-binder.sh failed"
    bash -n "$REPO/pending-deploy/newbox-baseline/baseline-newbox.sh" || fail "bash -n baseline-newbox.sh failed"
    bash -n "$REPO/install.sh" || fail "bash -n install.sh failed"
    bash -n "$REPO/wing/install-wing.sh" || fail "bash -n install-wing.sh failed"
    bash -n "$REPO/pending-deploy/state-migrate/import-state.sh" || fail "bash -n import-state.sh failed"
    bash -n "$REPO/waydroid-image/entrypoint.sh" || fail "bash -n entrypoint.sh failed"
    bash -n "$REPO/pending-deploy/apply-all/vps-apply.sh" || fail "bash -n vps-apply.sh failed"
    log "preflight ok"
}

build_stage() {
    STAGE_LOCAL="/tmp/jerusalem-deploy-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$STAGE_LOCAL/stage" || fail "mkdir stage"
    i=0
    for src in $SOURCES; do
        i=$((i + 1))
        name="$(echo "$STAGE_NAMES" | cut -d' ' -f"$i")"
        cp -f "$REPO/$src" "$STAGE_LOCAL/stage/$name" || fail "stage copy $src"
    done
    cp -f "$REPO/pending-deploy/apply-all/vps-apply.sh" "$STAGE_LOCAL/vps-apply.sh"
    chmod +x "$STAGE_LOCAL/vps-apply.sh"
    {
        echo "built=$(date -u +%FT%TZ) git=$GIT_HASH repo=$REPO"
        echo "vps=$VPS remote_base=$REMOTE_BASE"
        (cd "$STAGE_LOCAL/stage" && sha256sum $STAGE_NAMES)
    } > "$STAGE_LOCAL/MANIFEST"
    log "stage built: $STAGE_LOCAL"
}

print_manual() {
    cat <<EOF
[deploy-all] --stage-only: run these yourself (or re-run without the flag):
  $SSH $SSH_OPTS $VPS "mkdir -p $REMOTE_BASE"
  $SCP -r $STAGE_LOCAL $VPS:$REMOTE_BASE/
  $SSH $SSH_OPTS $VPS "DEPLOY_GIT_HASH=$GIT_HASH bash $REMOTE_BASE/$(basename "$STAGE_LOCAL")/vps-apply.sh"
EOF
}

main() {
    if [ "$SKIP_PREFLIGHT" = 0 ]; then
        preflight
    else
        GIT_HASH="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
        log "preflight SKIPPED (--skip-preflight)"
    fi
    build_stage
    if [ "$STAGE_ONLY" = 1 ]; then
        print_manual
        return 0
    fi
    if [ "$ASSUME_YES" = 0 ]; then
        echo "[deploy-all] Will deploy 19 files to $VPS:$REMOTE_BASE then run vps-apply.sh"
        echo "[deploy-all] (backup -> install -> restart jerusalem -> verify -> rollback on failure)"
        printf "[deploy-all] Proceed? [y/N] "
        read -r ans
        [ "$ans" = "y" ] || [ "$ans" = "Y" ] || fail "aborted by user"
    fi
    log "copying stage to $VPS:$REMOTE_BASE/"
    # mkdir first, always: `scp -r src host:dir/` NESTS the source dir when
    # the target exists but RENAMES it onto the target when it doesn't. The
    # first run therefore flattened ($REMOTE_BASE/vps-apply.sh) while every
    # later run nests ($REMOTE_BASE/jerusalem-deploy-<ts>/...) — and the ssh
    # command below would then execute the FIRST run's vps-apply.sh + stage,
    # silently re-deploying stale code while the log claims the new git hash.
    # The deterministic nested layout makes every run execute the stage it
    # just uploaded.
    # shellcheck disable=SC2086
    $SSH $SSH_OPTS "$VPS" "mkdir -p '$REMOTE_BASE'" || fail "ssh mkdir failed"
    $SCP $SSH_OPTS -r "$STAGE_LOCAL" "$VPS:$REMOTE_BASE/" || fail "scp failed"
    REMOTE_RUN_DIR="$REMOTE_BASE/$(basename "$STAGE_LOCAL")"
    log "running vps-apply.sh on $VPS ($REMOTE_RUN_DIR)"
    # shellcheck disable=SC2086
    $SSH $SSH_OPTS "$VPS" "DEPLOY_GIT_HASH=$GIT_HASH bash $REMOTE_RUN_DIR/vps-apply.sh"
    rc=$?
    case "$rc" in
        0) log "SUCCESS: all staged deploys applied and verified" ;;
        1) fail "vps-apply pre-deploy failure (nothing was touched on the VPS)" ;;
        2) fail "vps-apply deploy failure — AUTOMATIC ROLLBACK completed (old code restored)" ;;
        3) fail "vps-apply deploy failure AND ROLLBACK VERIFICATION FAILED — manual intervention needed on $VPS" ;;
        *) fail "vps-apply exited $rc (ssh/transport problem?)" ;;
    esac
    return 0
}

main
