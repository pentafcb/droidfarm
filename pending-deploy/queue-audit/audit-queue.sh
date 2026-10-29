#!/bin/bash
# audit-queue.sh — mechanical consistency audit for the pending-deploy queue.
#
# The queue's wiring is manually synced across five places:
#   pending-deploy/<pkg>/        (the packages: DEPLOY.md + test suites)
#   apply-all/deploy-all.sh      (preflight suite list, SOURCES/STAGE_NAMES)
#   apply-all/vps-apply.sh       (KEYS stage allowlist)
#   apply-all/APPLY.md           (docs: what's included, suite list)
#   live-verify/verify-live.sh   (--ssh probes: every staged file must be
#                                 checked, or a partial deploy reports "all good")
# Drift here is silent and dangerous: a deploy that skips a new package's
# tests, ships a stale snapshot copy, stages an unexpected file set, or
# leaves a staged file unprobed by the live-verifier.
# This script fails closed on any mismatch. Run it by hand, or let the
# deploy-all.sh preflight run it (it does).
#
# Checks:
#   1. every package dir (except apply-all, queue-audit) has a non-empty DEPLOY.md
#   2. every test_* under pending-deploy/*/ is invoked in the deploy-all.sh
#      preflight exactly once — and every invoked test path exists
#      (apply-all/test_apply_all.sh is exempt: it tests the orchestrator
#      itself and runs separately)
#   3. deploy-all.sh SOURCES/STAGE_NAMES stage-file set == vps-apply.sh KEYS
#      allowlist, and every SOURCES path exists in the repo
#   4. SNAPSHOTS manifests: any staged copy of a canonical repo file is
#      byte-identical to the canonical source (stale snapshots ship old code)
#   5. APPLY.md names every package (docs must not drift); every
#      code-deploying package appears in the "What's included" section
#      (python-security-patch is deliberately NOT code-deployed -> exempt
#      from the included-section check, but must still be mentioned)
#   6. no legacy pre-rename paths/units in deploy scripts or DEPLOY.md
#      (the 2026-10-08 rename moved the manager dir + systemd unit; a
#      surviving old path ships a broken deploy)
#   7. no unmarked old product-name refs in deploy scripts, DEPLOY.md, or
#      APPLY.md (a verify step grepping the log for the pre-rename banner
#      name fails spuriously on a healthy deploy)
#   8. live-verify/verify-live.sh --ssh probes every STAGE_NAMES file
#      (a staged file it never checks is a blind spot: a partial deploy
#      failing on exactly that file still reports "LIVE MATCHES HEAD")
#
# Env: REPO   (default: repo root derived from this script's location)
#      PD_DIR (default: $REPO/pending-deploy)
# Exit 0 = all green; 1 = findings (each printed as "FAIL: ...").
set -u
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="${REPO:-$(cd "$SELF_DIR/../.." && pwd)}"
PD_DIR="${PD_DIR:-$REPO/pending-deploy}"
DEPLOY_ALL="$PD_DIR/apply-all/deploy-all.sh"
VPS_APPLY="$PD_DIR/apply-all/vps-apply.sh"
APPLY_MD="$PD_DIR/apply-all/APPLY.md"

fails=0
fail() { echo "FAIL: $1"; fails=$((fails + 1)); }
pass() { echo "ok: $1"; }

[ -d "$PD_DIR" ] || { fail "pending-deploy dir missing: $PD_DIR"; echo "AUDIT: $fails finding(s)"; exit 1; }
[ -f "$DEPLOY_ALL" ] || { fail "deploy-all.sh missing"; echo "AUDIT: $fails finding(s)"; exit 1; }
[ -f "$VPS_APPLY" ] || { fail "vps-apply.sh missing"; echo "AUDIT: $fails finding(s)"; exit 1; }
[ -f "$APPLY_MD" ] || { fail "APPLY.md missing"; echo "AUDIT: $fails finding(s)"; exit 1; }

# package dirs, excluding the orchestrator and this auditor itself
pkgs=""
for d in "$PD_DIR"/*/; do
    [ -d "$d" ] || continue
    b="$(basename "$d")"
    case "$b" in apply-all|queue-audit) continue;; esac
    pkgs="$pkgs $b"
done

# --- check 1: DEPLOY.md present and non-empty in every package ---
for p in $pkgs; do
    if [ ! -s "$PD_DIR/$p/DEPLOY.md" ]; then
        fail "package $p: DEPLOY.md missing or empty"
    fi
done
pass "check 1: DEPLOY.md present in all packages"

# --- check 2: preflight invokes every test suite exactly once ---
# discovered test files, repo-relative to PD_DIR, minus the orchestrator's own
discovered="$(cd "$PD_DIR" && find . -maxdepth 2 -name 'test_*' -type f \
    | sed 's|^\./||' | grep -v '^apply-all/' | sort)"
# invoked in preflight: paths matching pending-deploy/<pkg>/test_*,
# normalized to PD_DIR-relative so they compare with $discovered
invoked="$(grep -o 'pending-deploy/[A-Za-z0-9_.-]*/test_[A-Za-z0-9_.]*' "$DEPLOY_ALL" \
    | grep -v '^pending-deploy/apply-all/' | sed 's|^pending-deploy/||' | sort)"
for t in $discovered; do
    n="$(printf '%s\n' "$invoked" | grep -cx "$t" || true)"
    if [ "$n" -eq 0 ]; then
        fail "test $t exists but is NOT invoked in deploy-all.sh preflight (suite silently skipped)"
    elif [ "$n" -gt 1 ]; then
        fail "test $t invoked $n times in deploy-all.sh preflight (want exactly once)"
    fi
done
while IFS= read -r t; do
    [ -z "$t" ] && continue
    if [ ! -f "$PD_DIR/$t" ]; then
        fail "deploy-all.sh preflight invokes $t but the file does not exist"
    fi
done <<< "$invoked"
pass "check 2: preflight suite wiring matches discovered test files"

# --- check 3: stage-file set agreement between deploy-all.sh and vps-apply.sh ---
SOURCES="$(sed -n 's/^SOURCES="\(.*\)"/\1/p' "$DEPLOY_ALL")"
STAGE_NAMES="$(sed -n 's/^STAGE_NAMES="\(.*\)"/\1/p' "$DEPLOY_ALL")"
KEYS="$(sed -n 's/^KEYS="\(.*\)"/\1/p' "$VPS_APPLY")"
[ -n "$SOURCES" ] || fail "could not parse SOURCES from deploy-all.sh"
[ -n "$STAGE_NAMES" ] || fail "could not parse STAGE_NAMES from deploy-all.sh"
[ -n "$KEYS" ] || fail "could not parse KEYS from vps-apply.sh"
n_src="$(printf '%s' "$SOURCES" | wc -w)"
n_names="$(printf '%s' "$STAGE_NAMES" | wc -w)"
[ "$n_src" -eq "$n_names" ] || fail "SOURCES ($n_src) / STAGE_NAMES ($n_names) count mismatch in deploy-all.sh"
i=0
stage_set=""
for src in $SOURCES; do
    i=$((i + 1))
    [ -f "$REPO/$src" ] || fail "SOURCES entry missing from repo: $src"
    name="$(printf '%s' "$STAGE_NAMES" | cut -d' ' -f"$i")"
    stage_set="$stage_set $name"
done
# set comparison, order-insensitive
a="$(printf '%s' "$stage_set" | tr ' ' '\n' | grep -v '^$' | sort | tr '\n' ' ')"
b="$(printf '%s' "$KEYS" | tr ' ' '\n' | grep -v '^$' | sort | tr '\n' ' ')"
[ "$a" = "$b" ] || fail "stage file set mismatch: deploy-all.sh stages{$a} vs vps-apply.sh KEYS{$b}"
pass "check 3: stage-file set agreed (deploy-all.sh == vps-apply.sh)"

# --- check 4: SNAPSHOTS manifests — staged copies byte-identical to canonicals ---
for p in $pkgs; do
    man="$PD_DIR/$p/SNAPSHOTS"
    [ -f "$man" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue;; esac
        case "$line" in
            *=*) staged="${line%%=*}"; canon="${line#*=}";;
            *) fail "package $p: malformed SNAPSHOTS line: $line"; continue;;
        esac
        [ -n "$staged" ] && [ -n "$canon" ] || { fail "package $p: empty side in SNAPSHOTS line: $line"; continue; }
        if [ ! -f "$REPO/$staged" ]; then
            fail "package $p: SNAPSHOTS staged file missing: $staged"
        elif [ ! -f "$REPO/$canon" ]; then
            fail "package $p: SNAPSHOTS canonical file missing: $canon"
        elif ! cmp -s "$REPO/$staged" "$REPO/$canon"; then
            fail "package $p: STALE snapshot $staged != canonical $canon (refresh it before deploy)"
        fi
    done < "$man"
done
pass "check 4: staged snapshots in sync with canonical sources"

# --- check 5: APPLY.md mentions every package; included-section covers code pkgs ---
# NOTE (2026-10-18): the name match is WHOLE-WORD (-w). A plain substring
# match let the untracked pending-deploy/android-optimize/ stub (a harmful
# 2-file entrypoint.patch using the verified-broken `settings put`) pass as
# "documented" purely because its name is a strict prefix of the real
# android-optimizer package row. A stub whose name merely collides with a
# longer documented name must fail closed here instead.
included_section="$(awk 'BEGIN{insec=0} /^## /{insec=($0 ~ /What.s included/)} insec{print}' "$APPLY_MD")"
[ -n "$included_section" ] || fail "APPLY.md has no 'What's included' section"
for p in $pkgs; do
    grep -qw -- "$p" "$APPLY_MD" || fail "package $p: not mentioned anywhere in APPLY.md (docs drifted)"
    case "$p" in
        python-security-patch) continue;;  # deliberately not code-deployed
    esac
    printf '%s' "$included_section" | grep -qw -- "$p" \
        || fail "package $p: missing from APPLY.md 'What's included' (docs drifted)"
done
pass "check 5: APPLY.md covers all packages"

# --- check 6: no legacy pre-rename paths/units in deploy scripts or DEPLOY.md ---
# The 2026-10-08 rename moved the manager directory and its systemd unit.
# A surviving old host path in a deploy script would fail the vps-apply
# preflight (missing dir) or, worse, scatter files into a dead directory;
# a surviving old unit name in docs/scripts would send an operator at the
# wrong service. Scan every package-level .sh (deploy scripts) and every
# DEPLOY.md. Exemptions: test_* files (they may legitimately exercise the
# legacy guard), and lines marked legacy-guard (the vps-apply preflight
# guard that aborts when the pre-rename unit is still active).
legacy_bad=0
for f in "$PD_DIR"/*/*.sh "$PD_DIR"/*/DEPLOY.md; do
    [ -f "$f" ] || continue
    case "$f" in */test_*) continue;; esac
    while IFS= read -r line; do
        case "$line" in *[Ll][Ee][Gg][Aa][Cc][Yy]*) continue;; esac
        case "$line" in
            */root/droidfarm*) # legacy-guard pattern: matches the pre-rename manager dir
                fail "legacy path in ${f#$PD_DIR/}: $line"; legacy_bad=1;;
        esac
        case "$line" in
            *systemctl*droidfarm*|*journalctl*droidfarm*) # legacy-guard pattern: pre-rename unit refs
                case "$line" in *droidfarm-*) ;; # e.g. droidfarm-wing: not the manager unit
                    *) fail "legacy unit ref in ${f#$PD_DIR/}: $line"; legacy_bad=1;;
                esac;;
        esac
    done < "$f"
done
[ "$legacy_bad" = 0 ] && pass "check 6: no legacy pre-rename paths/units in deploy scripts/docs"

# --- check 7: no unmarked old product-name refs in deploy scripts/docs ---
# Check 6 covers old VPS paths/units, but the old product NAME survives in
# prose and verify steps: a DEPLOY.md that greps the service log for the
# pre-rename banner name (the banner has said "Jerusalem 1.0" since the
# rename) fails its verify step spuriously on a healthy deploy (caught
# 2026-10-09 in h264-mse/DEPLOY.md). Flag every exact old product name in
# package-level .sh (deploy scripts), DEPLOY.md files, and APPLY.md.
# Exemptions: test_* files, legacy-marked lines, and rename-history lines.
name_bad=0
for f in "$PD_DIR"/*/*.sh "$PD_DIR"/*/DEPLOY.md "$APPLY_MD"; do
    [ -f "$f" ] || continue
    case "$f" in */test_*) continue;; esac
    while IFS= read -r line; do
        case "$line" in *[Ll][Ee][Gg][Aa][Cc][Yy]*) continue;; esac
        case "$line" in *[Rr]enam*) continue;; esac
        case "$line" in
            *DroidFarm*) # legacy-name pattern: the exact pre-rename product name
                fail "old product name in ${f#$PD_DIR/}: $line"; name_bad=1;;
        esac
    done < "$f"
done
[ "$name_bad" = 0 ] && pass "check 7: no unmarked old product-name refs in deploy scripts/docs"

# --- check 8: live-verify --ssh probes every staged file ---
# verify-live.sh Part 2 answers "what's actually live on the box". A staged
# file it never probes is a blind spot: a partial deploy failing on exactly
# that file still reports "LIVE MATCHES HEAD" (caught 2026-10-12 ~11:45 UTC
# for repoint-backup-cron.sh/screencap-loop.sh/push-screencap.sh, and again
# the same day for finish-deploy.sh — both times the file was added to the
# stage list after the tool was last wired). Every STAGE_NAMES entry must be
# covered: either named in the remote `for f in` probe loop, or
# df-watchdog.sh via its dedicated SHA_watchdog probe (it is installed to
# /usr/local/bin, outside /root/jerusalem).
lv_bad=0
lv="$PD_DIR/live-verify/verify-live.sh"
if [ ! -f "$lv" ]; then
    fail "live-verify/verify-live.sh missing (check 8 needs it)"; lv_bad=1
else
    probe_list="$(sed -n 's/^for f in \(.*\); do$/\1/p' "$lv")"
    if [ -z "$probe_list" ]; then
        fail "check 8: could not parse the remote probe file list from verify-live.sh"; lv_bad=1
    else
        # the for-list holds paths relative to /root/jerusalem
        # (static/index.html, wing/agent.py); STAGE_NAMES holds basenames
        probe_base="$(printf '%s' "$probe_list" | tr ' ' '\n' | sed 's|.*/||' | tr '\n' ' ')"
        for name in $STAGE_NAMES; do
            case "$name" in
                df-watchdog.sh)
                    grep -q 'SHA_watchdog=' "$lv" || \
                        { fail "check 8: $name has no probe in verify-live.sh (want the SHA_watchdog branch)"; lv_bad=1; };;
                *)
                    case " $probe_base " in
                        *" $name "*) ;;
                        *) fail "check 8: staged file $name is never probed by verify-live.sh --ssh (add it to the remote for-list)"; lv_bad=1;;
                    esac;;
            esac
        done
    fi
fi
[ "$lv_bad" = 0 ] && pass "check 8: live-verify --ssh probes every staged file"

# --- check 9: finish-deploy.sh only runs staged scripts ---
# Caught 2026-10-13: finish-deploy.sh steps 1-2 ran $DF_DIR/push-keepalive.sh
# and $DF_DIR/push-booted-watcher.sh, but apply-all never staged either file —
# on the VPS both steps failed with "No such file" and the whole post-deploy
# run aborted. The orchestrator's own preflight couldn't see it (its STAGED
# list didn't include them either) and the hermetic test planted them by hand.
# Every "$DF_DIR/"*.sh the orchestrator invokes must be a STAGE_NAMES entry,
# and its STAGED preflight list must cover every staged file (except
# finish-deploy.sh itself, which is obviously present when it runs).
fd_bad=0
fd="$PD_DIR/post-deploy/finish-deploy.sh"
if [ ! -f "$fd" ]; then
    fail "post-deploy/finish-deploy.sh missing (check 9 needs it)"; fd_bad=1
else
    fd_refs="$(grep -o '\$DF_DIR/[A-Za-z0-9_.-]*\.sh' "$fd" | sed 's|.*/||' | sort -u)"
    if [ -z "$fd_refs" ]; then
        fail "check 9: no \$DF_DIR/*.sh references parsed from finish-deploy.sh"; fd_bad=1
    else
        for ref in $fd_refs; do
            case " $STAGE_NAMES " in
                *" $ref "*) ;;
                *) fail "check 9: finish-deploy.sh runs \$DF_DIR/$ref but it is not staged (add to SOURCES/STAGE_NAMES/KEYS)"; fd_bad=1;;
            esac
        done
    fi
    staged_list="$(sed -n 's/^STAGED="\(.*\)"$/\1/p' "$fd")"
    if [ -z "$staged_list" ]; then
        fail "check 9: could not parse the STAGED preflight list from finish-deploy.sh"; fd_bad=1
    else
        for name in $STAGE_NAMES; do
            [ "$name" = "finish-deploy.sh" ] && continue
            case " $staged_list " in
                *" $name "*) ;;
                *) fail "check 9: staged file $name is not in finish-deploy.sh STAGED preflight (a partial stage would slip through)"; fd_bad=1;;
            esac
        done
    fi
fi
[ "$fd_bad" = 0 ] && pass "check 9: finish-deploy.sh only runs staged scripts"

# --- check 10: Roblox pin agreement ---
# The fleet's pinned Roblox version is defined in four places that must
# agree: the main-node drift guard (waydroid-image/df-watchdog.sh
# PINNED_ROBLOX default), the wing drift guard (wing/agent.py
# PINNED_ROBLOX default), and the image-build scripts
# (fetch-roblox-apks.sh documents the pin its sha256 pins belong to;
# extract-roblox-apks.sh's default XAPK path names the version). A partial
# pin bump — drift guards moved to a new version while the image build
# still bakes the old splits, or vice versa — would flag every instance as
# drifted, or miss real drift. Fails closed when a definition is missing
# or unparsable: a refactored pin line must update this check, not slip
# past it.
#
# Second half: the pinned split SHAs are hard-coded in TWO image-build
# files (fetch-roblox-apks.sh's PIN_BASE_SHA_DEFAULT / PIN_X86_SHA_DEFAULT
# and vps-image-build.sh's copies, which gate the morning image build).
# A pin bump that updates one file's SHAs but not the other's either
# breaks the image build with a confusing sha256 mismatch or verifies
# against stale pins. Both files must carry identical values.
pin_bad=0
pin_want=""
pin_one() { # $1=file $2=label $3=grep-E-pattern $4=sed-E-extract
    local f="$1" label="$2" vers n
    if [ ! -f "$f" ]; then
        fail "check 10: $label missing: $f"; pin_bad=1; return
    fi
    vers="$(grep -Eo "$3" "$f" 2>/dev/null | sed -E "$4" | sort -u)"
    n="$(printf '%s\n' "$vers" | grep -c .)"
    if [ "$n" -eq 0 ]; then
        fail "check 10: cannot extract Roblox pin from $label ($f)"; pin_bad=1; return
    fi
    if [ "$n" -gt 1 ]; then
        fail "check 10: conflicting Roblox pins in $label: $(printf '%s' "$vers" | tr '\n' ' ')"
        pin_bad=1; return
    fi
    if [ -z "$pin_want" ]; then
        pin_want="$vers"
    elif [ "$vers" != "$pin_want" ]; then
        fail "check 10: Roblox pin mismatch: $label pins $vers, want $pin_want"; pin_bad=1
    fi
}
pin_one "$REPO/waydroid-image/df-watchdog.sh" "df-watchdog.sh PINNED_ROBLOX" \
    'DF_PINNED_ROBLOX:-[0-9][0-9.]*' 's/.*:-//'
pin_one "$REPO/wing/agent.py" "wing/agent.py PINNED_ROBLOX" \
    'DF_PINNED_ROBLOX", "[0-9][0-9.]*"' 's/.*", "//; s/"$//'
pin_one "$PD_DIR/waydroid-image-build/fetch-roblox-apks.sh" "fetch-roblox-apks.sh documented pin" \
    'pinned( Roblox)? [0-9][0-9.]*' 's/^.*pinned (Roblox )?//'
pin_one "$PD_DIR/waydroid-image-build/extract-roblox-apks.sh" "extract-roblox-apks.sh default XAPK" \
    'roblox-[0-9][0-9.]*-x86_64\.xapk' 's/^roblox-//; s/-x86_64\.xapk$//'
[ "$pin_bad" = 0 ] && pass "check 10: Roblox pin agrees ($pin_want)"

pin_sha_bad=0
pin_sha_one() { # $1 = BASE|X86
    local role="$1" fsha vsha
    fsha="$(grep -Eo "PIN_${role}_SHA_DEFAULT=\"[0-9a-f]{64}\"" "$PD_DIR/waydroid-image-build/fetch-roblox-apks.sh" 2>/dev/null | sed -E 's/.*="//; s/"$//' | sort -u)"
    vsha="$(grep -Eo "PIN_${role}_SHA_DEFAULT=\"[0-9a-f]{64}\"" "$PD_DIR/waydroid-image-build/vps-image-build.sh" 2>/dev/null | sed -E 's/.*="//; s/"$//' | sort -u)"
    if [ "$(printf '%s\n' "$fsha" | grep -c .)" -ne 1 ]; then
        fail "check 10: fetch-roblox-apks.sh PIN_${role}_SHA_DEFAULT missing or duplicated"; pin_sha_bad=1; return
    fi
    if [ "$(printf '%s\n' "$vsha" | grep -c .)" -ne 1 ]; then
        fail "check 10: vps-image-build.sh PIN_${role}_SHA_DEFAULT missing or duplicated"; pin_sha_bad=1; return
    fi
    if [ "$fsha" != "$vsha" ]; then
        fail "check 10: PIN_${role}_SHA_DEFAULT mismatch: fetch-roblox-apks.sh vs vps-image-build.sh"; pin_sha_bad=1
    fi
}
pin_sha_one BASE
pin_sha_one X86
[ "$pin_sha_bad" = 0 ] && pass "check 10: Roblox SHA pins agree (fetch == vps-image-build)"

echo "AUDIT: $fails finding(s)"
[ "$fails" -eq 0 ]
