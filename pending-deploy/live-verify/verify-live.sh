#!/bin/bash
# verify-live.sh — one-command live-state triage for the Jerusalem 1.0 farm.
#
# Answers: is the VPS reachable, is the manager answering, and does what's
# LIVE match this repo's HEAD (or how far behind is it)? Built after the
# 2026-10-10 ~13:35 UTC incident, when the dashboard went unreachable from
# worker sessions and nothing on hand could say whether that was an egress
# artifact or a real outage — or which of the 30 staged queue packages were
# actually live on the box.
#
# Part 1 (always runs): unauthenticated HTTP probes against the dashboard.
#   No credentials needed; safe to run from anywhere (worker session, laptop).
# Part 2 (--ssh): root checks over ssh (BatchMode, one connection). For the
#   next SSH-capable session; worker sessions don't have VPS SSH.
#
# Usage: verify-live.sh [--host HOST] [--port PORT] [--ssh] [--repo DIR]
#                       [--timeout SEC]
#   --host    VPS address (default 209.222.10.93)
#   --port    dashboard port (default 8080)
#   --ssh     also run the root checks over ssh as root@HOST
#   --repo    repo root for HEAD file comparison (default: script's repo)
#   --timeout curl/ssh connect timeout seconds (default 8)
#
# Exit: 0 = reachable and live matches HEAD (warnings allowed),
#       1 = unreachable, service down, or live differs from HEAD,
#       2 = usage error.
# Output lines: [OK] / [FAIL] / [WARN], then a VERDICT line.
# Never prints secrets: only SHAs, versions, counts, and paths.
set -u

HOST="209.222.10.93"
PORT="8080"
DO_SSH=0
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TIMEOUT="8"
for a in "$@"; do
    case "$a" in
        --host=*) HOST="${a#--host=}" ;;
        --port=*) PORT="${a#--port=}" ;;
        --ssh) DO_SSH=1 ;;
        --repo=*) REPO="${a#--repo=}" ;;
        --timeout=*) TIMEOUT="${a#--timeout=}" ;;
        --host|--port|--repo|--timeout) echo "flag $a needs =value" >&2; exit 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown flag: $a" >&2; exit 2 ;;
    esac
done

CURL="${VERIFY_CURL:-curl}"
SSH="${VERIFY_SSH:-ssh}"
BASE="http://$HOST:$PORT"
FAIL=0
WARN=0

ok()   { echo "[OK] $1"; }
fail() { echo "[FAIL] $1"; FAIL=$((FAIL+1)); }
warn() { echo "[WARN] $1"; WARN=$((WARN+1)); }

# hprobe <path> [curl flags...]
# Sets HPROBE_CODE / HPROBE_SECS / HPROBE_RC. Returns 0 if any HTTP answer
# arrived — even when curl then exits non-zero (e.g. exit 18: the server
# answered 200 but closed mid-body). A truncated 200 is reachable-but-
# degraded, NOT "unreachable": 2026-10-11 the manager answered HTTP 200 on /
# while truncating the 56KB page ~60% of the time, and the old code called
# that UNREACHABLE and exited before the route probes ran.
hprobe() {
    local path="$1"; shift
    local out rc
    out="$("$CURL" -s -o /dev/null -w "%{http_code} %{time_total}" \
            --max-time "$TIMEOUT" "$@" "$BASE$path" 2>/dev/null)"
    rc=$?
    HPROBE_RC="$rc"
    if [ -n "$out" ]; then
        HPROBE_CODE="${out%% *}"; HPROBE_SECS="${out##* }"
        return 0
    fi
    HPROBE_CODE="curlfail"; HPROBE_SECS="-"
    return 1
}

echo "== Jerusalem 1.0 live verify: $BASE =="

# H1: dashboard root reachable
if hprobe "/"; then
    if [ "$HPROBE_CODE" = "200" ]; then
        ok "dashboard reachable (HTTP 200, ${HPROBE_SECS}s)"
        if [ "${HPROBE_RC:-0}" != "0" ]; then
            warn "dashboard answered 200 but the transfer did not complete (curl exit ${HPROBE_RC}) — large responses are being truncated; the manager may be wedged or starved. Route probes below still ran."
        fi
    else
        fail "dashboard answered HTTP $HPROBE_CODE (expected 200) — manager may be wedged or proxying wrong"
    fi
else
    fail "dashboard unreachable (no HTTP answer within ${TIMEOUT}s)"
    echo "VERDICT: VPS UNREACHABLE from here (cannot distinguish worker-egress filtering from a real outage — check from an SSH-capable session or the phone app)"
    exit 1
fi

# H2: served index.html currency vs repo HEAD.
# Only a COMPLETE fetch (curl exit 0, non-empty body, byte-exact via a temp
# file) is comparable: a truncated body hashes differently on every attempt
# and would false-positive as "BEHIND HEAD" (observed 2026-10-11: four
# fetches, four different SHAs, all truncation artifacts). Retries 3x, then
# reports inconclusive instead of failing.
INDEX_LOCAL="$REPO/manager/static/index.html"
if [ -f "$INDEX_LOCAL" ]; then
    _idxf="$(mktemp)"
    _idx_complete=0; _idx_tries=0
    while [ "$_idx_tries" -lt 3 ] && [ "$_idx_complete" = "0" ]; do
        _idx_tries=$((_idx_tries+1))
        if "$CURL" -s --max-time "$TIMEOUT" "$BASE/static/index.html" \
                >"$_idxf" 2>/dev/null && [ -s "$_idxf" ]; then
            _idx_complete=1
        fi
    done
    if [ "$_idx_complete" = "1" ]; then
        SERVED_SHA="$(sha256sum < "$_idxf" | cut -d' ' -f1)"
        rm -f "$_idxf"
        LOCAL_SHA="$(sha256sum < "$INDEX_LOCAL" | cut -d' ' -f1)"
        if [ "$SERVED_SHA" = "$LOCAL_SHA" ]; then
            ok "served index.html matches repo HEAD (${LOCAL_SHA:0:12})"
        else
            fail "served index.html BEHIND HEAD (live ${SERVED_SHA:0:12} vs repo ${LOCAL_SHA:0:12}) — dashboard queue items not yet deployed"
        fi
    else
        rm -f "$_idxf"
        warn "could not fetch a complete /static/index.html (${_idx_tries} truncated attempts) — currency vs HEAD inconclusive (truncation, not drift)"
    fi
else
    warn "repo file missing ($INDEX_LOCAL) — skipping currency comparison"
fi

# H3: /api/instances auth gate
if hprobe "/api/instances"; then
    case "$HPROBE_CODE" in
        401) ok "/api/instances auth gate up (401 unauthenticated)" ;;
        200) warn "/api/instances returned 200 unauthenticated — auth gate missing?" ;;
        *)   fail "/api/instances returned HTTP $HPROBE_CODE" ;;
    esac
else
    fail "/api/instances probe failed at curl level"
fi

# H4: bulk-accounts route (401 = live, 404 = predates it)
if hprobe "/api/roblox-accounts"; then
    case "$HPROBE_CODE" in
        401) ok "bulk-accounts route live (/api/roblox-accounts -> 401)" ;;
        404) warn "/api/roblox-accounts -> 404: live predates the bulk-accounts package" ;;
        *)   fail "/api/roblox-accounts returned HTTP $HPROBE_CODE" ;;
    esac
else
    fail "/api/roblox-accounts probe failed at curl level"
fi

# H5: watchdog-events route — newest route; 404 means live predates it
if hprobe "/api/watchdog-events?lines=1"; then
    case "$HPROBE_CODE" in
        401) ok "watchdog-events route live (newest queue route present)" ;;
        404) warn "/api/watchdog-events -> 404: live predates the watchdog-events package" ;;
        *)   fail "/api/watchdog-events returned HTTP $HPROBE_CODE" ;;
    esac
else
    fail "/api/watchdog-events probe failed at curl level"
fi

# H6: h264 websocket route
if hprobe "/ws/h264/rbx1" -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="; then
    case "$HPROBE_CODE" in
        401) ok "h264 route live (/ws/h264/rbx1 -> 401)" ;;
        501) ok "h264 route live but no ffmpeg on VPS (-> 501, MJPEG fallback by design)" ;;
        404) warn "/ws/h264/rbx1 -> 404: live predates the h264-mse package" ;;
        *)   fail "/ws/h264/rbx1 returned HTTP $HPROBE_CODE" ;;
    esac
else
    fail "/ws/h264/rbx1 probe failed at curl level"
fi

if [ "$DO_SSH" -eq 0 ]; then
    if [ "$FAIL" -eq 0 ]; then
        echo "VERDICT: HTTP LIVE (ssh checks skipped — rerun with --ssh from an SSH-capable session for file-level currency)"
    else
        echo "VERDICT: HTTP PROBES FOUND $FAIL FAILURE(S) (see FAIL lines above)"
    fi
    [ "$FAIL" -eq 0 ]
    exit $?
fi

echo "== SSH root checks: root@$HOST =="
REMOTE_OUT="$("$SSH" -o BatchMode=yes -o ConnectTimeout="$TIMEOUT" "root@$HOST" \
    'sh -s' <<'REMOTE_EOF' 2>/dev/null
_svc="$(systemctl is-active jerusalem 2>/dev/null)"; [ -n "$_svc" ] || _svc=unknown
echo "SVC=$_svc"
for f in app.py static/index.html h264_stream.py wing/agent.py wing/install-wing.sh wing/install-wing.bat roblox-keepalive.sh booted-watcher.sh backup-state.sh verify-backup.sh repoint-backup-cron.sh screencap-loop.sh push-screencap.sh finish-deploy.sh push-keepalive.sh push-booted-watcher.sh android-optimize.sh push-optimizer.sh; do
    p="/root/jerusalem/$f"
    key="$(printf '%s' "$f" | tr '/.' '__')"
    if [ -f "$p" ]; then
        echo "SHA_$key=$(sha256sum < "$p" | cut -d' ' -f1)"
    else
        echo "SHA_$key=missing"
    fi
done
if [ -f /usr/local/bin/df-watchdog.sh ]; then
    echo "SHA_watchdog=$(sha256sum < /usr/local/bin/df-watchdog.sh | cut -d' ' -f1)"
else
    echo "SHA_watchdog=missing"
fi
echo "AGENT_VERSION=$(grep -m1 '^VERSION' /root/jerusalem/wing/agent.py 2>/dev/null | cut -d'"' -f2)"
echo "FFMPEG=$(command -v ffmpeg >/dev/null 2>&1 && echo yes || echo no)"
echo "BINDER=$(ls /dev/binder* 2>/dev/null | wc -l)"
echo "LOOP=$(ls /dev/loop[0-9]* 2>/dev/null | wc -l)"
echo "CONTAINERS=$(docker ps --filter 'name=wd-' --format '{{.Names}}' 2>/dev/null | wc -l)"
echo "MARK_APP_WEVENTS=$(grep -c 'watchdog-events' /root/jerusalem/app.py 2>/dev/null || echo 0)"
echo "MARK_APP_FRAMELESS=$(grep -c 'WS_FRAMELESS_TIMEOUT' /root/jerusalem/app.py 2>/dev/null || echo 0)"
echo "MARK_INDEX_EVENTOS=$(grep -c 'Eventos' /root/jerusalem/static/index.html 2>/dev/null || echo 0)"
echo "CRON_WD=$( (crontab -l 2>/dev/null; ls /etc/cron.d/ 2>/dev/null) | grep -c 'df-watchdog' || true )"
# Watchdog liveness: the minute-cron watchdog touches this heartbeat on every
# executed run (after its v11 lock, so a silent-exit second run never fakes
# it). Age is computed server-side (same clock as the heartbeat write).
_WD_HB_TS="$(stat -c %Y /tmp/df-watchdog/heartbeat 2>/dev/null || echo missing)"
if [ "$_WD_HB_TS" = "missing" ]; then _WD_HB_AGE=missing; else _WD_HB_AGE=$(($(date +%s) - _WD_HB_TS)); fi
echo "WD_HB_AGE=$_WD_HB_AGE"
_cd="$(systemctl is-active cron 2>/dev/null)"; [ -n "$_cd" ] || _cd=unknown
echo "CRON_DAEMON=$_cd"
BACKUP_LINE="$(grep -rh 'backup' /etc/cron.d/ /var/spool/cron/crontabs/ 2>/dev/null | head -1)"
[ -z "$BACKUP_LINE" ] && BACKUP_LINE="$(crontab -l 2>/dev/null | grep 'backup' | head -1)"
echo "BACKUP_LINE=$(printf '%s' "$BACKUP_LINE" | cut -c1-160)"
echo "BACKUP_MARKER=$(python3 -c 'import json,time; d=json.load(open("/root/jerusalem/last-backup.json")); age=(time.time()-int(d["ts_epoch"]))/3600.0; err=str(d.get("error",""))[:60]; print("fail:%.1f:%s"%(age,err) if not d.get("ok") else ("stale:%.1f"%age if age>26 else "ok:%.1f:%d"%(age,int(d.get("bytes",0))//1048576)))' 2>/dev/null || echo missing)"
REMOTE_EOF
)"
if [ -z "$REMOTE_OUT" ]; then
    fail "ssh to root@$HOST failed (no SSH in this session or host unreachable)"
    echo "VERDICT: HTTP LIVE but SSH CHECKS UNAVAILABLE"
    exit 1
fi

rv() { printf '%s' "$REMOTE_OUT" | grep -m1 "^$1=" | cut -d'=' -f2-; }

# Service state
case "$(rv SVC)" in
    active) ok "jerusalem.service active" ;;
    *) fail "jerusalem.service is '$(rv SVC)' (expected active)" ;;
esac

# File currency vs repo HEAD
check_file() { # local-path remote-key label
    local local="$REPO/$1" remote_key="$2" label="$3" rsha lsha
    rsha="$(rv "$remote_key")"
    if [ "$rsha" = "missing" ] || [ -z "$rsha" ]; then
        fail "$label missing on VPS"
        return
    fi
    if [ ! -f "$local" ]; then warn "repo file missing ($1) — skipping"; return; fi
    lsha="$(sha256sum < "$local" | cut -d' ' -f1)"
    if [ "$rsha" = "$lsha" ]; then
        ok "$label matches HEAD (${lsha:0:12})"
    else
        fail "$label DIFFERS from HEAD (live ${rsha:0:12} vs repo ${lsha:0:12})"
    fi
}
check_file "manager/app.py"                 "SHA_app_py"            "manager/app.py"
check_file "manager/static/index.html"       "SHA_static_index_html" "manager/static/index.html"
check_file "manager/h264_stream.py"          "SHA_h264_stream_py"    "manager/h264_stream.py"
check_file "wing/agent.py"                   "SHA_wing_agent_py"     "served wing/agent.py"
check_file "wing/install-wing.sh"           "SHA_wing_install-wing_sh" "/root/jerusalem/wing/install-wing.sh"
check_file "wing/install-wing.bat"          "SHA_wing_install-wing_bat" "/root/jerusalem/wing/install-wing.bat"
check_file "waydroid-image/roblox-keepalive.sh" "SHA_roblox-keepalive_sh" "roblox-keepalive.sh"
check_file "waydroid-image/booted-watcher.sh"   "SHA_booted-watcher_sh"   "booted-watcher.sh"
check_file "waydroid-image/df-watchdog.sh"   "SHA_watchdog"          "/usr/local/bin/df-watchdog.sh"
check_file "pending-deploy/backup-state/backup-state.sh"  "SHA_backup-state_sh"  "/root/jerusalem/backup-state.sh"
check_file "pending-deploy/backup-state/verify-backup.sh" "SHA_verify-backup_sh" "/root/jerusalem/verify-backup.sh"
check_file "pending-deploy/backup-cron-repoint/repoint-backup-cron.sh" "SHA_repoint-backup-cron_sh" "/root/jerusalem/repoint-backup-cron.sh"
check_file "waydroid-image/screencap-loop.sh" "SHA_screencap-loop_sh" "/root/jerusalem/screencap-loop.sh"
check_file "pending-deploy/adaptive-screencap/push-screencap.sh" "SHA_push-screencap_sh" "/root/jerusalem/push-screencap.sh"
check_file "pending-deploy/post-deploy/finish-deploy.sh" "SHA_finish-deploy_sh" "/root/jerusalem/finish-deploy.sh"
check_file "pending-deploy/keepalive-v2/push-keepalive.sh" "SHA_push-keepalive_sh" "/root/jerusalem/push-keepalive.sh"
check_file "pending-deploy/booted-watcher/push-booted-watcher.sh" "SHA_push-booted-watcher_sh" "/root/jerusalem/push-booted-watcher.sh"
check_file "waydroid-image/android-optimize.sh" "SHA_android-optimize_sh" "/root/jerusalem/android-optimize.sh"
check_file "pending-deploy/android-optimizer/push-optimizer.sh" "SHA_push-optimizer_sh" "/root/jerusalem/push-optimizer.sh"

av="$(rv AGENT_VERSION)"
[ -n "$av" ] && ok "served wing agent VERSION=$av" || warn "could not read served agent VERSION"

# Feature markers (quick read even when SHAs differ)
[ "$(rv MARK_APP_WEVENTS)" -gt 0 ] 2>/dev/null && ok "app.py carries the watchdog-events route" \
    || warn "app.py lacks the watchdog-events route"
[ "$(rv MARK_APP_FRAMELESS)" -gt 0 ] 2>/dev/null && ok "app.py carries the frameless guard" \
    || warn "app.py lacks the frameless guard"
[ "$(rv MARK_INDEX_EVENTOS)" -gt 0 ] 2>/dev/null && ok "index.html carries the Eventos modal" \
    || warn "index.html lacks the Eventos modal"

# ffmpeg: warning-only (MJPEG fallback is by design)
[ "$(rv FFMPEG)" = "yes" ] && ok "ffmpeg present (H.264 available)" \
    || warn "ffmpeg missing on VPS — streams stay on MJPEG (by design)"

# binder pool
b="$(rv BINDER)"
if [ "$b" -ge 60 ] 2>/dev/null; then ok "binder pool healthy ($b devices)"; else warn "binder pool low ($b devices, want >=60)"; fi

# loop pool (second kernel device pool: ~2 per Waydroid instance at boot,
# stock 8 caps a fleet at ~4 booting instances — warn-only like binder,
# the manager self-heals at create and boot-recovery restores at boot)
l="$(rv LOOP)"
if [ "$l" -ge 60 ] 2>/dev/null; then ok "loop pool healthy ($l devices)"; else warn "loop pool low ($l devices, want >=60)"; fi

# containers
c="$(rv CONTAINERS)"
if [ "$c" -ge 1 ] 2>/dev/null; then ok "$c wd-* containers running"; else warn "no wd-* containers running"; fi

# watchdog cron
[ "$(rv CRON_WD)" -ge 1 ] 2>/dev/null && ok "df-watchdog minute-cron installed" \
    || warn "df-watchdog cron entry not found"

# cron daemon: without it the watchdog + backup crons never fire
case "$(rv CRON_DAEMON)" in
    active) ok "cron daemon active" ;;
    *) warn "cron daemon is '$(rv CRON_DAEMON)' — the minute-cron watchdog and the backup cron never fire; baseline-newbox.sh now ensures cron" ;;
esac

# watchdog liveness: the heartbeat proves the minute-cron watchdog is actually
# FIRING, not merely deployed with an active cron daemon. A healthy no-op run
# logs nothing, so the log mtime can't prove liveness. WARN-only: operational
# flag for the SSH session, not a code-currency check.
_hba="$(rv WD_HB_AGE)"
if [ "$_hba" = "missing" ]; then
    warn "df-watchdog heartbeat missing — the minute cron has never fired (or the box predates the liveness touch; rerun baseline-newbox.sh)"
elif [ "$_hba" -gt 300 ] 2>/dev/null; then
    warn "df-watchdog heartbeat stale (${_hba}s) — the minute cron is not executing; check /etc/cron.d/jerusalem-watchdog"
else
    ok "df-watchdog firing (heartbeat ${_hba}s ago)"
fi

# backup cron repoint (post-rename: must reference /root/jerusalem;
# canonical form calls backup-state.sh — the old explicit tar file list
# silently skips roblox_login_results.json + batch_jobs/ journals)
bl="$(rv BACKUP_LINE)"
if [ -z "$bl" ]; then
    warn "no backup cron line found"
elif printf '%s' "$bl" | grep -q '/root/droidfarm'; then # legacy-guard: intentionally detects the pre-rename path surviving in the backup cron
    fail "backup cron still points at legacy pre-rename /root/droidfarm"
elif printf '%s' "$bl" | grep -q 'backup-state\.sh'; then
    ok "backup cron calls backup-state.sh (canonical)"
elif printf '%s' "$bl" | grep -q '/root/jerusalem'; then
    warn "backup cron targets /root/jerusalem but does not use backup-state.sh — its explicit file list skips roblox_login_results.json + batch_jobs/"
else
    warn "backup cron line found but targets neither path: ${bl:0:80}"
fi

# backup freshness (WARN-only): the 03:17 state backup is silent when it
# fails — backup-state.sh's last-backup.json marker is the only signal,
# and it also drives the dashboard's fleet-bar backup line.
bm="$(rv BACKUP_MARKER)"
case "$bm" in
    ok:*)   rest="${bm#ok:}";  ok "last state backup ${rest%%:*}h ago (${rest##*:} MB)" ;;
    fail:*) rest="${bm#fail:}"; warn "last state backup FAILED ${rest%%:*}h ago (error: ${rest#*:}) — see /var/log/jerusalem-backup.log" ;;
    stale:*) warn "last state backup is stale (${bm#stale:}h old) — the 03:17 cron may have stopped firing" ;;
    *)      warn "no last-backup.json marker — backup-state.sh may never have run on this box" ;;
esac

echo "== summary: FAIL=$FAIL WARN=$WARN =="
if [ "$FAIL" -eq 0 ]; then
    echo "VERDICT: LIVE MATCHES HEAD"
    exit 0
else
    echo "VERDICT: LIVE DIFFERS FROM HEAD ($FAIL failure(s)) — run pending-deploy/apply-all/deploy-all.sh from an SSH-capable session"
    exit 1
fi
