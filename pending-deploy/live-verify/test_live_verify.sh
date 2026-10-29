#!/bin/bash
# test_live_verify.sh — hermetic tests for verify-live.sh (fake curl + fake ssh;
# T38 additionally runs the real embedded remote heredoc through a real sh).
# No network, no VPS. Final line: PASS=$PASS FAIL=$FAIL (preflight greps it).
set -u

S="$(cd "$(dirname "$0")" && pwd)"
V="$S/verify-live.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "BAD: $1"; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/fakebin" "$T/repo/manager/static" "$T/repo/wing" "$T/repo/waydroid-image" "$T/repo/pending-deploy/backup-state" "$T/repo/pending-deploy/backup-cron-repoint" "$T/repo/pending-deploy/adaptive-screencap" "$T/repo/pending-deploy/post-deploy" "$T/repo/pending-deploy/keepalive-v2" "$T/repo/pending-deploy/booted-watcher" "$T/repo/pending-deploy/android-optimizer"

# ---- fixture repo (the "HEAD" files) ----
echo "head-index-v1" > "$T/repo/manager/static/index.html"
echo "head-app-v1" > "$T/repo/manager/app.py"
echo "head-h264-v1" > "$T/repo/manager/h264_stream.py"
echo "head-agent-v1" > "$T/repo/wing/agent.py"
echo "head-installwing-sh-v1" > "$T/repo/wing/install-wing.sh"
echo "head-installwing-bat-v1" > "$T/repo/wing/install-wing.bat"
echo "head-keepalive-v1" > "$T/repo/waydroid-image/roblox-keepalive.sh"
echo "head-booted-v1" > "$T/repo/waydroid-image/booted-watcher.sh"
echo "head-watchdog-v1" > "$T/repo/waydroid-image/df-watchdog.sh"
echo "head-backupstate-v1" > "$T/repo/pending-deploy/backup-state/backup-state.sh"
echo "head-verifybackup-v1" > "$T/repo/pending-deploy/backup-state/verify-backup.sh"
echo "head-repointcron-v1" > "$T/repo/pending-deploy/backup-cron-repoint/repoint-backup-cron.sh"
echo "head-screencaploop-v1" > "$T/repo/waydroid-image/screencap-loop.sh"
echo "head-pushscreencap-v1" > "$T/repo/pending-deploy/adaptive-screencap/push-screencap.sh"
echo "head-finishdeploy-v1" > "$T/repo/pending-deploy/post-deploy/finish-deploy.sh"
echo "head-pushkeepalive-v1" > "$T/repo/pending-deploy/keepalive-v2/push-keepalive.sh"
echo "head-pushbootedwatcher-v1" > "$T/repo/pending-deploy/booted-watcher/push-booted-watcher.sh"
echo "head-androadopt-v1" > "$T/repo/waydroid-image/android-optimize.sh"
echo "head-pushoptimizer-v1" > "$T/repo/pending-deploy/android-optimizer/push-optimizer.sh"
echo "stale-index-v0" > "$T/stale-index.html"

# ---- fake curl ----
cat > "$T/fakebin/curl" <<'EOF'
#!/bin/bash
url=""
for a in "$@"; do case "$a" in http*) url="$a";; esac; done
echo "$url" >> "${FC_URLLOG:-/dev/null}"
path="$(printf '%s' "$url" | sed 's|^http://[^/]*||')"
if [ -n "${FC_DOWN:-}" ]; then exit 28; fi
case "$path" in
  /) code="${FC_ROOT:-200}" ;;
  /static/index.html) code=200 ;;
  /api/instances) code="${FC_INSTANCES:-401}" ;;
  /api/roblox-accounts) code="${FC_ACCOUNTS:-401}" ;;
  /api/watchdog-events*) code="${FC_EVENTS:-401}" ;;
  /ws/h264/*) code="${FC_H264:-401}" ;;
  *) code=404 ;;
esac
if printf '%s\n' "$@" | grep -qx -- "-o"; then
  if [ -n "${FC_TRUNC_ROOT:-}" ] && [ "$path" = "/" ]; then
    printf '200 0.011'; exit 18
  fi
  printf '%s 0.011' "$code"
else
  if [ -n "${FC_TRUNC_INDEX:-}" ] && [ "$path" = "/static/index.html" ]; then
    head -c 40 "${FC_INDEX_BODY:-$FC_FIXTURE_INDEX}"; exit 18
  fi
  cat "${FC_INDEX_BODY:-$FC_FIXTURE_INDEX}"
fi
exit 0
EOF
chmod +x "$T/fakebin/curl"

# ---- fake ssh: ignores stdin, prints canned k=v ----
cat > "$T/fakebin/ssh" <<'EOF'
#!/bin/bash
if [ -n "${FC_SSH_FAIL:-}" ]; then exit 255; fi
cat "$FC_SSH_OUT"
exit 0
EOF
chmod +x "$T/fakebin/ssh"

export VERIFY_CURL="$T/fakebin/curl" VERIFY_SSH="$T/fakebin/ssh"
export FC_FIXTURE_INDEX="$T/repo/manager/static/index.html"
export FC_INDEX_BODY="$T/repo/manager/static/index.html"

run() { # run <expect_exit> <args...>; stdout -> $T/out
  local exp="$1"; shift
  FC_URLLOG="$T/urls.log" "$V" --repo="$T/repo" --host=1.2.3.4 --port=9999 "$@" > "$T/out" 2>&1
  local rc=$?
  [ "$rc" -eq "$exp" ] && ok "exit=$exp :: $*" || bad "exit=$rc want $exp :: $* :: $(tail -2 "$T/out" | tr '\n' '|')"
}
has() { grep -q -- "$1" "$T/out" && ok "output has [$1]" || bad "output missing [$1]"; }
nothas() { grep -q -- "$1" "$T/out" && bad "output should NOT have [$1]" || ok "output lacks [$1]"; }

# canned ssh output builder; env overrides: O_SVC O_APP_SHA O_FFMPEG O_BINDER O_LOOP O_BACKUP O_WD
mkssh() {
  local s
  s() { sha256sum < "$T/repo/$1" | cut -d' ' -f1; }
  cat > "$T/ssh_out" <<EOF
SVC=${O_SVC:-active}
SHA_app_py=${O_APP_SHA:-$(s manager/app.py)}
SHA_static_index_html=$(s manager/static/index.html)
SHA_h264_stream_py=$(s manager/h264_stream.py)
SHA_wing_agent_py=$(s wing/agent.py)
SHA_wing_install-wing_sh=$(s wing/install-wing.sh)
SHA_wing_install-wing_bat=$(s wing/install-wing.bat)
SHA_roblox-keepalive_sh=$(s waydroid-image/roblox-keepalive.sh)
SHA_booted-watcher_sh=$(s waydroid-image/booted-watcher.sh)
SHA_watchdog=$(s waydroid-image/df-watchdog.sh)
SHA_backup-state_sh=$(s pending-deploy/backup-state/backup-state.sh)
SHA_verify-backup_sh=$(s pending-deploy/backup-state/verify-backup.sh)
SHA_repoint-backup-cron_sh=$(s pending-deploy/backup-cron-repoint/repoint-backup-cron.sh)
SHA_screencap-loop_sh=$(s waydroid-image/screencap-loop.sh)
SHA_push-screencap_sh=$(s pending-deploy/adaptive-screencap/push-screencap.sh)
SHA_finish-deploy_sh=$(s pending-deploy/post-deploy/finish-deploy.sh)
SHA_push-keepalive_sh=$(s pending-deploy/keepalive-v2/push-keepalive.sh)
SHA_push-booted-watcher_sh=$(s pending-deploy/booted-watcher/push-booted-watcher.sh)
SHA_android-optimize_sh=$(s waydroid-image/android-optimize.sh)
SHA_push-optimizer_sh=$(s pending-deploy/android-optimizer/push-optimizer.sh)
AGENT_VERSION=1.0.5
FFMPEG=${O_FFMPEG:-yes}
BINDER=${O_BINDER:-64}
LOOP=${O_LOOP:-64}
CONTAINERS=20
MARK_APP_WEVENTS=1
MARK_APP_FRAMELESS=1
MARK_INDEX_EVENTOS=1
CRON_WD=1
CRON_DAEMON=${O_CRON:-active}
WD_HB_AGE=${O_WDHB:-30}
BACKUP_LINE=${O_BACKUP:-17 3 * * * root bash /root/jerusalem/backup-state.sh}
EOF
  export FC_SSH_OUT="$T/ssh_out"
}

echo "== T1 happy path, http-only =="
run 0
has "VERDICT: HTTP LIVE"
has "\[OK\] served index.html matches repo HEAD"
has "\[OK\] /api/instances auth gate up"

echo "== T2 served index.html behind HEAD =="
FC_INDEX_BODY="$T/stale-index.html" run 1
has "BEHIND HEAD"
has "VERDICT: HTTP PROBES FOUND"

echo "== T3 VPS down =="
FC_DOWN=1 run 1
has "VERDICT: VPS UNREACHABLE"
unset FC_DOWN

echo "== T4 dashboard root 502 =="
FC_ROOT=502 run 1
has "HTTP 502"
unset FC_ROOT

echo "== T5 /api/instances 200 unauthenticated -> WARN not FAIL =="
FC_INSTANCES=200 run 0
has "WARN.*auth gate missing"
unset FC_INSTANCES

echo "== T6 watchdog-events 404 -> WARN (predates package) =="
FC_EVENTS=404 run 0
has "predates the watchdog-events package"
unset FC_EVENTS

echo "== T7a h264 501 -> OK (no ffmpeg, by design) =="
FC_H264=501 run 0
has "MJPEG fallback by design"
unset FC_H264

echo "== T7b h264 404 -> WARN =="
FC_H264=404 run 0
has "predates the h264-mse package"
unset FC_H264

echo "== T8 unknown flag -> exit 2 =="
"$V" --bogus > "$T/out" 2>&1
[ "$?" -eq 2 ] && ok "exit=2 on unknown flag" || bad "unknown flag exit=$?"

echo "== T9 --ssh happy path =="
mkssh
run 0 --ssh
has "VERDICT: LIVE MATCHES HEAD"
has "\[OK\] jerusalem.service active"
has "\[OK\] manager/app.py matches HEAD"
has "\[OK\] /usr/local/bin/df-watchdog.sh matches HEAD"
has "VERSION=1.0.5"
has "\[OK\] ffmpeg present"
has "binder pool healthy (64 devices)"
has "loop pool healthy (64 devices)"
has "20 wd-\* containers running"
has "\[OK\] backup cron calls backup-state.sh (canonical)"
has "\[OK\] /root/jerusalem/backup-state.sh matches HEAD"
has "\[OK\] /root/jerusalem/verify-backup.sh matches HEAD"
has "\[OK\] /root/jerusalem/repoint-backup-cron.sh matches HEAD"
has "\[OK\] /root/jerusalem/screencap-loop.sh matches HEAD"
has "\[OK\] /root/jerusalem/push-screencap.sh matches HEAD"
has "\[OK\] /root/jerusalem/finish-deploy.sh matches HEAD"
has "\[OK\] /root/jerusalem/push-keepalive.sh matches HEAD"
has "\[OK\] /root/jerusalem/push-booted-watcher.sh matches HEAD"
has "\[OK\] /root/jerusalem/android-optimize.sh matches HEAD"
has "\[OK\] /root/jerusalem/push-optimizer.sh matches HEAD"
has "\[OK\] /root/jerusalem/wing/install-wing.sh matches HEAD"
has "\[OK\] /root/jerusalem/wing/install-wing.bat matches HEAD"
has "\[OK\] cron daemon active"

echo "== T10 --ssh service inactive =="
O_SVC=inactive mkssh
run 1 --ssh
has "jerusalem.service is 'inactive'"
unset O_SVC

echo "== T11 --ssh app.py differs =="
O_APP_SHA=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef mkssh
run 1 --ssh
has "manager/app.py DIFFERS from HEAD"
unset O_APP_SHA

echo "== T12 --ssh ffmpeg missing -> WARN, still exit 0 =="
O_FFMPEG=no mkssh
run 0 --ssh
has "ffmpeg missing on VPS"
unset O_FFMPEG

echo "== T13 --ssh binder pool low -> WARN =="
O_BINDER=26 mkssh
run 0 --ssh
has "binder pool low (26 devices"
unset O_BINDER

echo "== T14 --ssh connection fails =="
FC_SSH_FAIL=1 run 1 --ssh
has "ssh to root@1.2.3.4 failed"
unset FC_SSH_FAIL

echo "== T15 --ssh backup cron pre-rename -> FAIL =="
O_BACKUP="17 3 * * * root /usr/local/bin/droidfarm-backup.sh /root/droidfarm" mkssh
run 1 --ssh
has "backup cron still points at legacy pre-rename /root/droidfarm"
unset O_BACKUP

echo "== T15b --ssh old-style explicit-list backup cron -> WARN, exit 0 =="
O_BACKUP="17 3 * * * root tar -czf /root/backups/jf.tar.gz -C /root/jerusalem instances.json" mkssh
run 0 --ssh
has "does not use backup-state.sh"
unset O_BACKUP

echo "== T16 --ssh watchdog file missing -> FAIL =="
mkssh
sed -i 's/^SHA_watchdog=.*/SHA_watchdog=missing/' "$T/ssh_out"
run 1 --ssh
has "/usr/local/bin/df-watchdog.sh missing on VPS"

echo "== T17 host/port reflected in probes =="
: > "$T/urls.log"
FC_URLLOG="$T/urls.log" "$V" --repo="$T/repo" --host=9.9.9.9 --port=1234 > /dev/null 2>&1
grep -q "http://9.9.9.9:1234/" "$T/urls.log" && ok "probes hit 9.9.9.9:1234" || bad "host/port not used"

echo "== T18 missing local index.html -> WARN skip, exit 0 =="
mv "$T/repo/manager/static/index.html" "$T/repo/manager/static/index.html.bak"
run 0
has "skipping currency comparison"
mv "$T/repo/manager/static/index.html.bak" "$T/repo/manager/static/index.html"

echo "== T19 watchdog-events 500 -> FAIL =="
FC_EVENTS=500 run 1
has "returned HTTP 500"
unset FC_EVENTS

echo "== T20 --help =="
"$V" --help > "$T/out" 2>&1
[ "$?" -eq 0 ] && ok "--help exit 0" || bad "--help exit=$?"

echo "== T21 root answers 200 but truncates (curl 18) -> reachable-but-degraded, exit 0 =="
FC_TRUNC_ROOT=1 run 0
has "\[OK\] dashboard reachable"
has "did not complete"
has "VERDICT: HTTP LIVE"
unset FC_TRUNC_ROOT

echo "== T22 /static/index.html truncated x3 -> inconclusive WARN, no BEHIND HEAD =="
FC_TRUNC_INDEX=1 run 0
has "inconclusive"
nothas "BEHIND HEAD"
has "VERDICT: HTTP LIVE"
unset FC_TRUNC_INDEX

echo "== T23 --ssh screencap-loop.sh missing -> FAIL =="
mkssh
sed -i 's/^SHA_screencap-loop_sh=.*/SHA_screencap-loop_sh=missing/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/screencap-loop.sh missing on VPS"

echo "== T24 --ssh push-screencap.sh differs -> FAIL =="
mkssh
sed -i 's/^SHA_push-screencap_sh=.*/SHA_push-screencap_sh=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/push-screencap.sh DIFFERS from HEAD"

echo "== T25 --ssh repoint-backup-cron.sh missing -> FAIL =="
mkssh
sed -i 's/^SHA_repoint-backup-cron_sh=.*/SHA_repoint-backup-cron_sh=missing/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/repoint-backup-cron.sh missing on VPS"

echo "== T26 --ssh finish-deploy.sh missing -> FAIL =="
mkssh
sed -i 's/^SHA_finish-deploy_sh=.*/SHA_finish-deploy_sh=missing/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/finish-deploy.sh missing on VPS"

echo "== T27 --ssh finish-deploy.sh differs -> FAIL =="
mkssh
sed -i 's/^SHA_finish-deploy_sh=.*/SHA_finish-deploy_sh=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/finish-deploy.sh DIFFERS from HEAD"

echo "== T28 --ssh push-keepalive.sh missing -> FAIL =="
mkssh
sed -i 's/^SHA_push-keepalive_sh=.*/SHA_push-keepalive_sh=missing/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/push-keepalive.sh missing on VPS"

echo "== T29 --ssh push-booted-watcher.sh differs -> FAIL =="
mkssh
sed -i 's/^SHA_push-booted-watcher_sh=.*/SHA_push-booted-watcher_sh=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/push-booted-watcher.sh DIFFERS from HEAD"

echo "== T30 --ssh android-optimize.sh missing -> FAIL =="
mkssh
sed -i 's/^SHA_android-optimize_sh=.*/SHA_android-optimize_sh=missing/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/android-optimize.sh missing on VPS"

echo "== T31 --ssh push-optimizer.sh differs -> FAIL =="
mkssh
sed -i 's/^SHA_push-optimizer_sh=.*/SHA_push-optimizer_sh=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/push-optimizer.sh DIFFERS from HEAD"

echo "== T32 --ssh cron daemon inactive -> WARN, exit 0 =="
O_CRON=inactive mkssh
run 0 --ssh
has "cron daemon is 'inactive'"
unset O_CRON

echo "== T33 --ssh cron daemon unknown -> WARN, exit 0 =="
O_CRON=unknown mkssh
run 0 --ssh
has "cron daemon is 'unknown'"
unset O_CRON

echo "== T39 --ssh watchdog heartbeat fresh -> OK, exit 0 =="
mkssh
run 0 --ssh
has "df-watchdog firing (heartbeat 30s ago)"
unset O_WDHB

echo "== T40 --ssh watchdog heartbeat missing -> WARN, exit 0 =="
O_WDHB=missing mkssh
run 0 --ssh
has "df-watchdog heartbeat missing"
unset O_WDHB

echo "== T41 --ssh watchdog heartbeat stale -> WARN, exit 0 =="
O_WDHB=400 mkssh
run 0 --ssh
has "df-watchdog heartbeat stale (400s)"
unset O_WDHB

echo "== T42 remote WD_HB_AGE is computed server-side (no client clock skew) =="
# The age must use the SERVER's clock: stat and date both run inside the
# remote heredoc. A client-side $(date) here would false-WARN whenever the
# repo machine's clock drifts from the VPS.
grep -q '_WD_HB_AGE=$(($(date +%s) - _WD_HB_TS))' "$V" && ok "WD_HB_AGE computed from server-side date" \
  || bad "WD_HB_AGE lost its server-side date computation"

echo "== T34 --ssh install-wing.sh missing -> FAIL =="
mkssh
sed -i 's/^SHA_wing_install-wing_sh=.*/SHA_wing_install-wing_sh=missing/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/wing/install-wing.sh missing on VPS"

echo "== T35 --ssh install-wing.bat differs -> FAIL =="
mkssh
sed -i 's/^SHA_wing_install-wing_bat=.*/SHA_wing_install-wing_bat=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef/' "$T/ssh_out"
run 1 --ssh
has "/root/jerusalem/wing/install-wing.bat DIFFERS from HEAD"

echo "== T36 --ssh loop pool low -> WARN, exit 0 =="
O_LOOP=26 mkssh
run 0 --ssh
has "loop pool low (26 devices"
unset O_LOOP

echo "== T37 LOOP probe glob excludes loop-control =="
# regression guard for the retired verify-binder stub bug: `ls /dev/loop*`
# counts loop-control (inflated); the product line must use /dev/loop[0-9]*.
grep -q 'LOOP=$(ls /dev/loop\[0-9\]\*' "$V" && ok "LOOP probe uses /dev/loop[0-9]*" || bad "LOOP probe lost the [0-9] glob"
grep -q 'LOOP=$(ls /dev/loop\*' "$V" && bad "LOOP probe has a bare /dev/loop* (counts loop-control)" || ok "no bare /dev/loop* in LOOP probe"
D="$T/fakedev"; mkdir -p "$D"; touch "$D/loop0" "$D/loop1" "$D/loop-control" "$D/loop0p1"
[ "$(ls "$D"/loop[0-9]* 2>/dev/null | wc -l)" -eq 3 ] && ok "glob [0-9]* excludes loop-control (loop0 loop1 loop0p1)" || bad "glob semantics wrong: $(ls "$D"/loop[0-9]* 2>/dev/null)"

echo "== T38 --ssh heredoc runs through a REAL sh (quoting regression guard) =="
# The canned FC_SSH_OUT fakes never execute the embedded remote heredoc, so
# quoting bugs inside it are invisible to T1-T37. T38 extracts the real
# heredoc from verify-live.sh and pipes it through a real `sh -s` with
# faked systemctl/crontab/docker, then asserts the output is clean k=v.
# Regression: a missing closing quote on the CRON_WD line made the remote
# shell emit `echo CRON_DAEMON=active` instead of `CRON_DAEMON=active`, so
# every --ssh run warned "cron daemon is '' — the minute-cron watchdog and
# the backup cron never fire". Also fixed: `systemctl is-active X ||
# echo unknown` leaked a stray `unknown` line whenever a service was
# inactive-but-present (is-active prints the word AND exits non-zero).
awk "/<<'REMOTE_EOF'/{f=1;next}/^REMOTE_EOF$/{f=0}f" "$V" > "$T/remote_heredoc.sh"
[ -s "$T/remote_heredoc.sh" ] && ok "remote heredoc extracted from verify-live.sh" \
  || bad "remote heredoc extraction empty"
mkdir -p "$T/fakebin-remote"
printf '#!/bin/bash\necho active\n' > "$T/fakebin-remote/systemctl"
printf '#!/bin/bash\necho "* * * * * /usr/local/bin/df-watchdog.sh"\n' > "$T/fakebin-remote/crontab"
printf '#!/bin/bash\nexit 0\n' > "$T/fakebin-remote/docker"
printf '#!/bin/bash\nexit 1\n' > "$T/fakebin-remote/python3"
chmod +x "$T/fakebin-remote"/*
PATH="$T/fakebin-remote:/usr/bin:/bin" sh -s < "$T/remote_heredoc.sh" > "$T/remote_out" 2>/dev/null
rc=$?
[ "$rc" -eq 0 ] && ok "real sh -s runs the remote heredoc cleanly (exit 0)" \
  || bad "real sh -s failed on the remote heredoc (exit $rc)"
grep -q '^SVC=active$' "$T/remote_out" && ok "SVC=active as a clean k=v line" \
  || bad "SVC line wrong: $(grep '^SVC' "$T/remote_out" | head -1)"
grep -q '^CRON_DAEMON=active$' "$T/remote_out" && ok "CRON_DAEMON=active as a clean k=v line" \
  || bad "CRON_DAEMON line wrong (quoting regression?): $(grep 'CRON_DAEMON' "$T/remote_out" | head -1)"
grep -q '^CRON_WD=1$' "$T/remote_out" && ok "CRON_WD=1 as a clean k=v line" \
  || bad "CRON_WD line wrong: $(grep '^CRON_WD' "$T/remote_out" | head -1)"
grep -q '^echo ' "$T/remote_out" && bad "stray 'echo '-prefixed line in remote output" \
  || ok "no 'echo '-prefixed lines in remote output"
_badv="$(grep -vc '^[A-Za-z_][A-Za-z0-9_-]*=' "$T/remote_out" || true)"
[ "$_badv" -eq 0 ] && ok "every remote output line is KEY=value shaped" \
  || bad "$_badv non-k=v line(s): $(grep -v '^[A-Za-z_][A-Za-z0-9_-]*=' "$T/remote_out" | head -3 | tr '\n' '|')"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
