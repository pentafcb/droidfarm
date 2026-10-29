#!/bin/bash
# test_newbox_baseline.sh — hermetic tests for
# pending-deploy/newbox-baseline/baseline-newbox.sh
#
# Builds a fake VPS root (fake docker/systemctl/id/crontab on PATH, all
# install targets redirected via NB_* env overrides) and runs the REAL
# script plus the REAL backup-state.sh / verify-backup.sh through it.
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
NB="$REPO/pending-deploy/newbox-baseline/baseline-newbox.sh"
PASS=0; FAILC=0
ok()  { PASS=$((PASS+1)); }
bad() { FAILC=$((FAILC+1)); echo "TEST-FAIL: $*"; }

# --- fixture: fake VPS root --------------------------------------------------
mktree() {
    T="$(mktemp -d)"
    BIN="$T/bin"; mkdir -p "$BIN"
    printf '#!/bin/bash\nexit 0\n' > "$BIN/docker"; chmod +x "$BIN/docker"
    cat > "$BIN/systemctl" <<EOF
#!/bin/bash
if [ "\$1" = "is-active" ]; then
    [ -f "$T/svc-down" ] && echo "inactive" || echo "active"
    exit 0
fi
if [ "\$1" = "reload" ]; then
    echo "reload" >> "$T/reloads"
    [ -f "$T/reload-fail" ] && exit 1 || exit 0
fi
exit 0
EOF
    chmod +x "$BIN/systemctl"
    printf '#!/bin/bash\necho "${FAKE_UID:-0}"\n' > "$BIN/id"; chmod +x "$BIN/id"
    sed "s|@STATE@|$T/crontab.txt|" > "$BIN/crontab" <<'EOF'
#!/bin/bash
ST="@STATE@"
if [ "${1:-}" = "-l" ]; then [ -f "$ST" ] && cat "$ST"; exit 0; fi
echo "usage" >&2; exit 1
EOF
    chmod +x "$BIN/crontab"
    : > "$T/crontab.txt"
    # sources: the real staged files
    SD="$T/src"; mkdir -p "$SD"
    cp "$REPO/waydroid-image/df-watchdog.sh" "$SD/df-watchdog.sh"
    cp "$REPO/pending-deploy/backup-state/backup-state.sh" "$SD/backup-state.sh"
    cp "$REPO/pending-deploy/backup-state/verify-backup.sh" "$SD/verify-backup.sh"
    # fake state dir with a planted secret (must never appear in output)
    JD="$T/jerusalem"; mkdir -p "$JD"
    for f in instances.json providers.json tokens.json roblox_login_results.json; do
        echo "{\"fixture\":1}" > "$JD/$f"
    done
    echo '{"rbx1":{"username":"u","password":"supersecret123"}}' > "$JD/roblox_accounts.json"
    echo "tok" > "$JD/static_join_token"
    export PATH="$BIN:/usr/bin:/bin"
    export NB_CRON_D="$T/cron.d" NB_BINDIR="$T/bin2"
    export NB_DOCKER_JSON="$T/docker/daemon.json" NB_JDIR="$JD"
    export NB_BINDER_GLOB="$T/binder/binder*"
    export JERUSALEM_BACKUPS="$T/backups" JERUSALEM_BACKUP_LOG="$T/backup.log"
    export MIN_FREE_MB=0
}
run_nb() { # args passed through; captures to $OUT, rc to $RC
    OUT="$(NB_NO_RELOAD="${NB_NO_RELOAD:-0}" bash "$NB" "$@" 2>&1)"; RC=$?
}
reloads() { [ -f "$T/reloads" ] && wc -l < "$T/reloads" || echo 0; }

# --- arg / preflight failures ------------------------------------------------
mktree
run_nb
[ "$RC" = 2 ] && ok "no args -> exit 2" || bad "no args rc=$RC"
echo "$OUT" | grep -q "usage" && ok "usage printed" || bad "no usage"

run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" /nonexistent/v.sh
[ "$RC" = 1 ] && ok "missing source -> exit 1" || bad "missing source rc=$RC"
echo "$OUT" | grep -q "source missing: /nonexistent/v.sh" && ok "missing source named" || bad "missing source not named"

printf '#!/bin/bash\necho broken (((\n' > "$SD/bad-wd.sh"
run_nb "$SD/bad-wd.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 1 ] && ok "bash -n failure -> exit 1" || bad "bad source rc=$RC"
[ ! -e "$T/bin2/df-watchdog.sh" ] && ok "bad source not installed" || bad "bad source installed"
echo "$OUT" | grep -q "bash -n failed" && ok "bash -n failure named" || bad "bash -n not named"

FAKE_UID=1000 run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 1 ] && ok "non-root refused" || bad "non-root rc=$RC"
echo "$OUT" | grep -q "must run as root" && ok "non-root message" || bad "non-root message missing"
unset FAKE_UID

mv "$BIN/docker" "$BIN/docker.hidden"
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 1 ] && ok "docker missing -> exit 1" || bad "docker missing rc=$RC"
mv "$BIN/docker.hidden" "$BIN/docker"

# --- fresh install happy path ------------------------------------------------
mktree
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 0 ] && ok "fresh install exit 0" || bad "fresh install rc=$RC"
cmp -s "$SD/df-watchdog.sh" "$T/bin2/df-watchdog.sh" && ok "watchdog installed byte-identical" || bad "watchdog content"
[ "$(cat "$T/cron.d/jerusalem-watchdog")" = "* * * * * root $T/bin2/df-watchdog.sh" ] \
    && ok "watchdog cron exact" || bad "watchdog cron content"
[ "$(cat "$T/cron.d/jerusalem-backup")" = "17 3 * * * root bash $T/jerusalem/backup-state.sh" ] \
    && ok "backup cron exact" || bad "backup cron content"
python3 -c "
import json
d = json.load(open('$T/docker/daemon.json'))
assert d['log-driver'] == 'json-file', d
assert d['log-opts']['max-size'] == '10m', d
assert d['log-opts']['max-file'] == '3', d
" 2>/dev/null && ok "daemon.json rotation values" || bad "daemon.json values"
[ "$(reloads)" = 1 ] && ok "docker reloaded once" || bad "reload count $(reloads)"
cmp -s "$SD/backup-state.sh" "$T/jerusalem/backup-state.sh" && ok "backup-state.sh installed" || bad "backup-state.sh"
cmp -s "$SD/verify-backup.sh" "$T/jerusalem/verify-backup.sh" && ok "verify-backup.sh installed" || bad "verify-backup.sh"
[ "$(ls "$T/backups"/jerusalem-state-*.tar.gz 2>/dev/null | wc -l)" = 1 ] && ok "backup archive produced" || bad "no archive"
echo "$OUT" | grep -q "backup drill passed" && ok "drill passed" || bad "drill not passed"
echo "$OUT" | grep -q "supersecret123" && bad "SECRET LEAKED in output" || ok "no secret in output"
echo "$OUT" | grep -q "only 0 binder" && ok "binder WARN surfaced" || bad "binder warn missing"

# --- idempotent re-run ---------------------------------------------------------
_before_dj=$(sha256sum < "$T/docker/daemon.json" | cut -d' ' -f1)
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 0 ] && ok "re-run exit 0" || bad "re-run rc=$RC"
[ "$(reloads)" = 1 ] && ok "no second docker reload" || bad "reload count $(reloads) on re-run"
[ "$_before_dj" = "$(sha256sum < "$T/docker/daemon.json" | cut -d' ' -f1)" ] \
    && ok "daemon.json byte-identical on re-run" || bad "daemon.json rewritten"
[ ! -e "$T/bin2/df-watchdog.sh.prev" ] && ok "no .prev when identical" || bad "spurious .prev"
echo "$OUT" | grep -q "\[already\]" && ok "already-paths reported" || bad "no already-paths"

# --- watchdog change -> .prev ---------------------------------------------------
echo "# changed" >> "$SD/df-watchdog.sh"
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 0 ] && ok "changed-source exit 0" || bad "changed-source rc=$RC"
cmp -s "$SD/df-watchdog.sh" "$T/bin2/df-watchdog.sh" && ok "new watchdog installed" || bad "new watchdog"
grep -q "# changed" "$T/bin2/df-watchdog.sh.prev" && bad ".prev has NEW content" || ok ".prev keeps old content"
grep -q "df-watchdog" "$T/bin2/df-watchdog.sh.prev" && ok ".prev is the watchdog" || bad ".prev wrong file"

# --- daemon.json merge paths ----------------------------------------------------
mktree
mkdir -p "$T/docker"
printf '{"debug":true,"log-opts":{"max-size":"100m"}}' > "$T/docker/daemon.json"
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 0 ] && ok "merge exit 0" || bad "merge rc=$RC"
python3 -c "
import json
d = json.load(open('$T/docker/daemon.json'))
assert d['debug'] is True, 'existing key lost'
assert d['log-opts']['max-size'] == '10m', d
assert d['log-opts']['max-file'] == '3', d
" 2>/dev/null && ok "existing keys preserved + merged" || bad "merge wrong"
[ "$(reloads)" = 1 ] && ok "reload on change" || bad "no reload on change"

mktree
mkdir -p "$T/docker"
printf '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}' > "$T/docker/daemon.json"
_before=$(sha256sum < "$T/docker/daemon.json" | cut -d' ' -f1)
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 0 ] && ok "already-correct exit 0" || bad "already-correct rc=$RC"
[ "$_before" = "$(sha256sum < "$T/docker/daemon.json" | cut -d' ' -f1)" ] \
    && ok "already-correct untouched" || bad "already-correct rewritten"
[ "$(reloads)" = 0 ] && ok "no reload when unchanged" || bad "spurious reload"

mktree
mkdir -p "$T/docker"
printf '{not json' > "$T/docker/daemon.json"
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 1 ] && ok "invalid daemon.json -> exit 1" || bad "invalid json rc=$RC"
[ "$(cat "$T/docker/daemon.json")" = "{not json" ] && ok "invalid file untouched" || bad "invalid file clobbered"
[ "$(reloads)" = 0 ] && ok "no reload on invalid" || bad "reload on invalid"
echo "$OUT" | grep -q "refusing to clobber" && ok "fail-closed message" || bad "no fail-closed message"

mktree
touch "$T/reload-fail"
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 0 ] && ok "reload failure still exit 0" || bad "reload failure rc=$RC"
echo "$OUT" | grep -qi "WARNING.*reload docker failed" && ok "reload WARN surfaced" || bad "no reload WARN"

# --- binder thresholds -----------------------------------------------------------
mktree
mkdir -p "$T/binder"
for i in $(seq 1 65); do touch "$T/binder/binder$i"; done
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 0 ] && ok "65 binders exit 0" || bad "65 binders rc=$RC"
echo "$OUT" | grep -q "binder devices: 65" && ok "binder ok line" || bad "no binder ok"

# --- personal-crontab deference ----------------------------------------------------
mktree
printf '* * * * * root /usr/local/bin/df-watchdog.sh\n' > "$T/crontab.txt"
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 0 ] && ok "crontab-covered exit 0" || bad "crontab-covered rc=$RC"
[ ! -e "$T/cron.d/jerusalem-watchdog" ] && ok "cron.d not created when crontab covers" || bad "cron.d clobbered crontab setup"
echo "$OUT" | grep -q "personal crontab" && ok "deference reported" || bad "no deference note"

# --- drill failure ---------------------------------------------------------------
mktree
printf '#!/bin/bash\necho nope >&2\nexit 1\n' > "$SD/vb-fail.sh"; chmod +x "$SD/vb-fail.sh"
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/vb-fail.sh"
[ "$RC" = 1 ] && ok "drill failure -> exit 1" || bad "drill failure rc=$RC"
echo "$OUT" | grep -q "backup drill failed" && ok "drill FAIL surfaced" || bad "no drill FAIL"

# --- service warnings --------------------------------------------------------------
mktree
touch "$T/svc-down"
run_nb "$SD/df-watchdog.sh" "$SD/backup-state.sh" "$SD/verify-backup.sh"
[ "$RC" = 0 ] && ok "inactive service still exit 0" || bad "inactive service rc=$RC"
echo "$OUT" | grep -qi "WARNING.*jerusalem.service not active" && ok "service WARN" || bad "no service WARN"

# --- script hygiene -----------------------------------------------------------------
bash -n "$NB" && ok "bash -n clean" || bad "bash -n failed"

echo "$PASS passed, $FAILC failed"
[ "$FAILC" = 0 ]
