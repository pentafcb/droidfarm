#!/bin/bash
# test_android_optimizer.sh — hermetic tests for the android-optimizer package.
#
# Covers: the canonical waydroid-image/android-optimize.sh (working-set only,
# none of the known-broken commands), the Dockerfile COPY, the entrypoint.sh
# /opt install + legacy fallback parity, and push-optimizer.sh against fake
# docker/volumes (happy path, skips, run failures, stale-marker rejection,
# no secret leakage, no stray writes).
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
OPT="$REPO/waydroid-image/android-optimize.sh"
ENTRY="$REPO/waydroid-image/entrypoint.sh"
BC="$REPO/waydroid-image/boot-complete.sh"
DOCKERFILE="$REPO/waydroid-image/Dockerfile"
PUSH="$REPO/pending-deploy/android-optimizer/push-optimizer.sh"
FD="$REPO/pending-deploy/post-deploy/finish-deploy.sh"
PASS=0; FAILC=0
ok()  { PASS=$((PASS+1)); }
bad() { FAILC=$((FAILC+1)); echo "TEST-FAIL: $*"; }

# --- 1. canonical optimizer content -------------------------------------------
# non-comment lines only (headers document WHY the broken commands are absent)
code_only() { grep -v '^[[:space:]]*#' "$1"; }
[ -f "$OPT" ] || bad "android-optimize.sh missing"
bash -n "$OPT" 2>/dev/null || bad "android-optimize.sh bash -n failed"
forline="$(grep '^for p in' "$OPT")"
for pkg in com.android.printspooler com.android.wallpaper.livepicker com.android.stk com.android.cellbroadcastreceiver com.android.bookmarkprovider; do
    case "$forline" in *"$pkg"*) ok "debloat $pkg";; *) bad "debloat missing: $pkg";; esac
done
grep -q 'pm disable-user --user 0 "\$p"' "$OPT" && ok "pm disable-user invocation" || bad "pm disable-user invocation missing"
grep -q "svc power stayon true" "$OPT" && ok "stay-on" || bad "stay-on missing"
grep -q "dumpsys deviceidle disable" "$OPT" && ok "no-doze" || bad "no-doze missing"
grep -q "pm trim-caches 999G" "$OPT" && ok "trim-caches" || bad "trim-caches missing"
grep -q '\[optimize\] done' "$OPT" && ok "completion marker" || bad "no [optimize] done marker"
# the known-broken commands must NEVER appear in executable lines
# (AppOps NPE / SecurityException — documented in the header comments)
code_only "$OPT" | grep -q "settings put" && bad "settings put present (known broken: AppOps NPE)" || ok "no settings put"
code_only "$OPT" | grep -q "bg-dexopt-job" && bad "bg-dexopt-job present (known blocked)" || ok "no bg-dexopt-job"
code_only "$OPT" | grep -q "cmd settings" && bad "cmd settings present" || ok "no cmd settings"
code_only "$OPT" | grep -qi "password\s*=\|token\s*=" && bad "secret assignment in optimizer" || ok "no secret assignments"

# --- 2. Dockerfile carries it into the image ----------------------------------
grep -q '^COPY android-optimize.sh /opt/android-optimize.sh$' "$DOCKERFILE" \
    && ok "Dockerfile COPY android-optimize.sh" || bad "Dockerfile missing COPY"

# --- 3. boot-complete.sh installs from /opt with legacy fallback ---------------
# (moved out of entrypoint.sh by the boot-complete package: the entrypoint's
# fast path and the booted-watcher's late-boot path now share the one script)
grep -q 'cp -f "$OPT/android-optimize.sh" "$S/android-optimize.sh"' "$BC" \
    && ok "boot-complete installs from /opt" || bad "boot-complete missing /opt install"
grep -q 'WARN $OPT/android-optimize.sh missing' "$BC" \
    && ok "boot-complete WARN fallback" || bad "boot-complete missing WARN fallback"
# fallback heredoc must carry the same working set as the canonical file
# (comment/blank lines stripped from both — the header docs live in README.md)
strip_c() { grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$'; }
canon_body="$(strip_c < "$OPT")"
fallback_body="$(awk "/<<'OPT_EOF'/{flag=1;next}/^OPT_EOF$/{flag=0}flag" "$BC" | strip_c)"
[ -n "$canon_body" ] && [ "$canon_body" = "$fallback_body" ] \
    && ok "fallback parity with canonical" || bad "fallback differs from canonical body"
# no settings-put anywhere in the optimizer block of boot-complete.sh
opt_block="$(sed -n '/Android OS optimization/,/Android optimizer started/p' "$BC")"
printf '%s' "$opt_block" | grep -v '^[[:space:]]*#' | grep -q "settings put" \
    && bad "settings put in boot-complete optimizer block" || ok "boot-complete optimizer block clean"
bash -n "$BC" || bad "boot-complete.sh bash -n failed"
bash -n "$ENTRY" || bad "entrypoint.sh bash -n failed"
# the entrypoint's fast path must delegate to boot-complete.sh (that's what
# runs the install above on every boot)
grep -q 'sh /opt/boot-complete.sh' "$ENTRY" \
    && ok "entrypoint delegates to boot-complete.sh" || bad "entrypoint missing boot-complete delegation"
bash -n "$PUSH" || bad "push-optimizer.sh bash -n failed"

# --- 4. push-optimizer.sh, hermetic -------------------------------------------
mktree() {
    T="$(mktemp -d)"
    BIN="$T/bin"; mkdir -p "$BIN"
    VB="$T/volumes"
    for c in wd-ok wd-unbooted wd-badrun wd-stale; do
        mkdir -p "$VB/$c-data/_data/data/media/0/jerusalem"
    done
    touch "$VB/wd-ok-data/_data/data/media/0/jerusalem/booted"
    touch "$VB/wd-badrun-data/_data/data/media/0/jerusalem/booted"
    touch "$VB/wd-stale-data/_data/data/media/0/jerusalem/booted"
    # wd-unbooted: no booted marker (entrypoint covers it at boot)
    # wd-stale: an OLD completion marker — must not count as a fresh success
    { echo "[optimize] run at 2020-01-01"; echo "[optimize] done"; } \
        > "$VB/wd-stale-data/_data/data/media/0/jerusalem/optimize.log"
    # planted secret + instance data file: neither may leak nor change
    echo "SECRET_TOKEN_DO_NOT_PRINT" > "$VB/wd-ok-data/_data/data/media/0/jerusalem/optimize.log"
    echo "instance-data-untouchable" > "$VB/wd-ok-data/_data/data/media/0/jerusalem/some-data.txt"
    # fake docker: ps lists $FAKE_CONTAINERS; exec simulates waydroid runs
    cat > "$BIN/docker" <<'EOF'
#!/bin/bash
if [ "${1:-}" = "ps" ]; then printf '%s\n' $FAKE_CONTAINERS; exit 0; fi
if [ "${1:-}" = "exec" ]; then
    c="$2"; shift 2
    case " $* " in
        *android-optimize.sh*)
            case " $FAKE_FAIL " in *" $c "*) exit 3;; esac
            case " $FAKE_SILENT " in *" $c "*) exit 0;; esac
            lf="$FAKE_VOLBASE/$c-data/_data/data/media/0/jerusalem/optimize.log"
            { echo "[optimize] run at fake"; echo "[optimize] done"; } >> "$lf"
            exit 0 ;;
    esac
    exit 0
fi
exit 0
EOF
    chmod +x "$BIN/docker"
    printf '#!/bin/sh\necho canonical-optimizer\n' > "$T/optimizer-src.sh"
}

run_push() { # $1=src; env: FAKE_CONTAINERS FAKE_FAIL FAKE_SILENT; -> rc, out in $T/out.log
    FAKE_VOLBASE="$VB" VOLBASE="$VB" OPT_TIMEOUT=10 \
    PATH="$BIN:/usr/bin:/bin" \
    bash "$PUSH" "$1" > "$T/out.log" 2>&1
}

# 4a. happy path: booted ok, unbooted skipped
mktree
FAKE_CONTAINERS="wd-ok wd-unbooted" FAKE_FAIL="" FAKE_SILENT="" run_push "$T/optimizer-src.sh"; rc=$?
[ "$rc" = 0 ] && ok "happy path exit 0" || bad "happy path exit $rc"
grep -q "wd-ok: optimizer applied" "$T/out.log" && ok "wd-ok applied" || bad "wd-ok not applied"
grep -q "wd-unbooted: not booted yet, skipping" "$T/out.log" && ok "unbooted skipped" || bad "unbooted not skipped"
grep -q "applied to 1 instance(s), skipped 1" "$T/out.log" && ok "summary counts" || bad "summary counts wrong"
[ -f "$VB/wd-ok-data/_data/data/media/0/jerusalem/android-optimize.sh" ] \
    && ok "script copied into volume" || bad "script not copied"
grep -q "canonical-optimizer" "$VB/wd-ok-data/_data/data/media/0/jerusalem/android-optimize.sh" \
    && ok "copied content is the canonical src" || bad "copied content wrong"
grep -q "SECRET_TOKEN_DO_NOT_PRINT" "$T/out.log" && bad "secret leaked into output" || ok "no secret in output"
[ "$(cat "$VB/wd-ok-data/_data/data/media/0/jerusalem/some-data.txt")" = "instance-data-untouchable" ] \
    && ok "instance data untouched" || bad "instance data modified"
[ -f "$VB/wd-unbooted-data/_data/data/media/0/jerusalem/android-optimize.sh" ] \
    && bad "unbooted container was touched" || ok "unbooted container untouched"

# 4b. waydroid run failure -> FAIL, exit 1
mktree
FAKE_CONTAINERS="wd-badrun" FAKE_FAIL="wd-badrun" FAKE_SILENT="" run_push "$T/optimizer-src.sh"; rc=$?
[ "$rc" = 1 ] && ok "run failure -> exit 1" || bad "run failure exit $rc"
grep -q "wd-badrun: optimizer run FAILED" "$T/out.log" && ok "run failure reported" || bad "run failure not reported"

# 4c. stale marker: old [optimize] done but no NEW marker -> must FAIL
mktree
FAKE_CONTAINERS="wd-stale" FAKE_FAIL="" FAKE_SILENT="wd-stale" run_push "$T/optimizer-src.sh"; rc=$?
[ "$rc" = 1 ] && ok "stale marker -> exit 1" || bad "stale marker exit $rc"
grep -q "no new completion marker" "$T/out.log" && ok "stale marker reported" || bad "stale marker not reported"
[ "$(wc -l < "$VB/wd-stale-data/_data/data/media/0/jerusalem/optimize.log")" = 2 ] \
    && ok "stale log unmodified" || bad "stale log was modified"

# 4d. missing source -> fail closed before touching anything
mktree
FAKE_CONTAINERS="wd-ok" FAKE_FAIL="" FAKE_SILENT="" run_push "$T/nope.sh"; rc=$?
[ "$rc" = 1 ] && ok "missing src -> exit 1" || bad "missing src exit $rc"
grep -q "source missing" "$T/out.log" && ok "missing src reported" || bad "missing src not reported"
[ -f "$VB/wd-ok-data/_data/data/media/0/jerusalem/android-optimize.sh" ] \
    && bad "container touched despite missing src" || ok "nothing touched on missing src"

# 4e. syntax-broken source -> fail closed
mktree
printf '#!/bin/sh\nif [ broken\n' > "$T/bad-src.sh"
FAKE_CONTAINERS="wd-ok" FAKE_FAIL="" FAKE_SILENT="" run_push "$T/bad-src.sh"; rc=$?
[ "$rc" = 1 ] && ok "bad syntax src -> exit 1" || bad "bad syntax src exit $rc"
grep -q "syntax check failed" "$T/out.log" && ok "syntax failure reported" || bad "syntax failure not reported"

# 4f. copy failure -> FAIL (fake cp that always fails)
mktree
printf '#!/bin/bash\nexit 1\n' > "$BIN/cp"; chmod +x "$BIN/cp"
FAKE_CONTAINERS="wd-ok" FAKE_FAIL="" FAKE_SILENT="" run_push "$T/optimizer-src.sh"; rc=$?
[ "$rc" = 1 ] && ok "copy failure -> exit 1" || bad "copy failure exit $rc"
grep -q "wd-ok: copy FAILED" "$T/out.log" && ok "copy failure reported" || bad "copy failure not reported"

# --- 5. finish-deploy wiring contract -----------------------------------------
grep -q '"$DF_DIR/push-optimizer.sh"' "$FD" && ok "finish-deploy calls push-optimizer.sh" \
    || bad "finish-deploy missing push-optimizer.sh call"
grep -q '"$DF_DIR/android-optimize.sh"' "$FD" && ok "finish-deploy passes android-optimize.sh" \
    || bad "finish-deploy missing android-optimize.sh arg"
grep -q 'STAGED=.*push-optimizer.sh' "$FD" && ok "finish-deploy STAGED has push-optimizer.sh" \
    || bad "finish-deploy STAGED missing push-optimizer.sh"
grep -q 'STAGED=.*android-optimize.sh' "$FD" && ok "finish-deploy STAGED has android-optimize.sh" \
    || bad "finish-deploy STAGED missing android-optimize.sh"

echo
echo "$PASS passed, $FAILC failed"
[ "$FAILC" = 0 ]
