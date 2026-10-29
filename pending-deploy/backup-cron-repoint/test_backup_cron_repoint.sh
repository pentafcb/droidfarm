#!/bin/bash
# test_backup_cron_repoint.sh — hermetic tests for repoint-backup-cron.sh.
# Fake cron.d dir + fake `crontab` binary on PATH; no system cron touched.
# Final line format ("N passed, 0 failed") is what deploy-all.sh preflight greps.
set -u
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$SELF_DIR/repoint-backup-cron.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $1"; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/cron.d"

# fake crontab: BACKUP_FAKE_CRONTAB_FILE holds the "personal crontab"
export BACKUP_FAKE_CRONTAB_FILE="$T/fake-crontab"
touch "$BACKUP_FAKE_CRONTAB_FILE"
mkdir -p "$T/bin"
cat > "$T/bin/crontab" <<'EOF'
#!/bin/bash
F="${BACKUP_FAKE_CRONTAB_FILE:?}"
case "$1" in
  -l) cat "$F" 2>/dev/null || exit 1 ;;
  *)  cp -f "$1" "$F" ;;
esac
EOF
chmod +x "$T/bin/crontab"
export PATH="$T/bin:$PATH"

run() { # run <cron.d dir>; prints stdout, returns script exit code
    BACKUP_CROND_DIR="$1" BACKUP_CRONTAB=crontab bash "$SCRIPT"
}

echo "test: legacy backup line in cron.d is repointed"
printf 'PATH=/usr/bin\n17 3 * * * root tar -czf /root/backups/jf-$(date +\\%%F).tar.gz -C /root/droidfarm . # daily backup\n' > "$T/cron.d/jerusalem-backup"
run "$T/cron.d" > "$T/out" 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc (want 0)"
grep -q "repointed" "$T/out" && ok "reports repointed" || bad "no repointed report"
grep -q "PATH=/usr/bin" "$T/cron.d/jerusalem-backup" && ok "other lines intact" || bad "other lines damaged"
grep -q "tar -czf /root/backups/jf-" "$T/cron.d/jerusalem-backup" && grep -q -- "-C /root/jerusalem \." "$T/cron.d/jerusalem-backup" \
    && ok "source dir rewritten to /root/jerusalem" || bad "rewrite wrong: $(cat "$T/cron.d/jerusalem-backup")"
! grep -q "droidfarm" "$T/cron.d/jerusalem-backup" && ok "no legacy-guard dir left in file" || bad "legacy-guard dir survives"

echo "test: idempotent — second run is a no-op"
sha1="$(sha256sum "$T/cron.d/jerusalem-backup" | cut -d' ' -f1)"
run "$T/cron.d" > "$T/out2" 2>&1; rc=$?
sha2="$(sha256sum "$T/cron.d/jerusalem-backup" | cut -d' ' -f1)"
[ "$rc" -eq 0 ] && ok "exit 0 on re-run" || bad "exit $rc on re-run"
[ "$sha1" = "$sha2" ] && ok "file byte-identical after re-run" || bad "file changed on re-run"
grep -q "no-op" "$T/out2" && ok "reports no-op" || bad "no no-op report"

echo "test: /root/jerusalem-deploy lookalike is NOT rewritten"
printf '17 3 * * * root tar -czf /root/backups/x.tar.gz -C /root/jerusalem-deploy . # backup of stage\n' > "$T/cron.d/lookalike"
sha1="$(sha256sum "$T/cron.d/lookalike" | cut -d' ' -f1)"
run "$T/cron.d" > "$T/out3" 2>&1
sha2="$(sha256sum "$T/cron.d/lookalike" | cut -d' ' -f1)"
[ "$sha1" = "$sha2" ] && ok "lookalike untouched" || bad "lookalike rewritten: $(cat "$T/cron.d/lookalike")"

echo "test: non-backup legacy-guard line is left alone"
printf '17 4 * * * root /root/droidfarm/cleanup.sh\n' > "$T/cron.d/other"
run "$T/cron.d" > /dev/null 2>&1
grep -q "/root/droidfarm/cleanup.sh" "$T/cron.d/other" && ok "non-backup line untouched" || bad "non-backup line touched"

echo "test: end-of-line and subpath legacy-guard occurrences"
printf '17 3 * * * root /root/droidfarm/backup.sh # backup\n* * * * * root echo backup >> /var/log/x.log; cp -r /root/droidfarm /root/droidfarm\n' > "$T/cron.d/multi"
run "$T/cron.d" > "$T/out4" 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc"
grep -q "/root/jerusalem/backup.sh # backup" "$T/cron.d/multi" && ok "subpath rewritten" || bad "subpath not rewritten"
grep -q "cp -r /root/jerusalem /root/jerusalem$" "$T/cron.d/multi" && ok "end-of-line occurrence rewritten" || bad "end-of-line not rewritten"

echo "test: personal crontab legacy-guard line is repointed"
printf '17 3 * * * tar -czf /tmp/b.tgz -C /root/droidfarm . # backup\n0 5 * * * echo hi\n' > "$BACKUP_FAKE_CRONTAB_FILE"
run "$T/cron.d" > "$T/out5" 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc"
grep -q -- "-C /root/jerusalem \." "$BACKUP_FAKE_CRONTAB_FILE" && ok "crontab line repointed" || bad "crontab not repointed"
grep -q "0 5 \* \* \* echo hi" "$BACKUP_FAKE_CRONTAB_FILE" && ok "other crontab lines intact" || bad "crontab damaged"

echo "test: no backup cron anywhere -> warning, exit 0"
rm -f "$T/cron.d"/*
: > "$BACKUP_FAKE_CRONTAB_FILE"
run "$T/cron.d" > "$T/out6" 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc"
grep -qi "no backup cron line found" "$T/out6" && ok "warns about missing backup cron" || bad "no missing-cron warning"

echo "test: missing cron.d dir is tolerated"
run "$T/does-not-exist" > "$T/out7" 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "exit 0 with missing dir" || bad "exit $rc"

echo "test: unreadable cron file -> exit 2"
printf '17 3 * * * root tar -czf /tmp/b.tgz -C /root/droidfarm . # backup\n' > "$T/cron.d/locked"
chmod 000 "$T/cron.d/locked"
if [ "$(id -u)" -eq 0 ]; then
    echo "  skip (running as root: 000 file still readable)"
    PASS=$((PASS+1))
else
    run "$T/cron.d" > "$T/out8" 2>&1; rc=$?
    [ "$rc" -eq 2 ] && ok "exit 2 on unreadable file" || bad "exit $rc (want 2)"
fi
chmod 644 "$T/cron.d/locked"

echo "test: never prints secrets"
run "$T/cron.d" > "$T/out9" 2>&1
! grep -qi "password\|secret\|token" "$T/out9" && ok "no secret-like words in output" || bad "secret-like words in output"

echo "test: already-repointed cron.d file is a clean no-op"
printf '17 3 * * * root tar -czf /root/backups/jf.tar.gz -C /root/jerusalem . # daily backup\n' > "$T/cron.d/good"
sha1="$(sha256sum "$T/cron.d/good" | cut -d' ' -f1)"
run "$T/cron.d" > "$T/out10" 2>&1; rc=$?
sha2="$(sha256sum "$T/cron.d/good" | cut -d' ' -f1)"
[ "$rc" -eq 0 ] && [ "$sha1" = "$sha2" ] && ok "no-op, exit 0" || bad "changed or failed"

echo "test: bash -n clean"
bash -n "$SCRIPT" && ok "bash -n clean" || bad "bash -n failed"

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
