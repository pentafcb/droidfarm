#!/bin/bash
# test_verify_binder.sh — hermetic tests for verify-binder.sh.
#
# Every run builds a fake host root (dev/, modprobe.d/, modules file,
# binderfs/, sys/module/) so nothing touches the real /dev or /etc.
# Fixture device nodes are regular files (no mknod needed); the script's
# char-device check is WARN-only, asserted in T12.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/verify-binder.sh"
REPO="$(cd "$HERE/../.." && pwd)"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "ok   $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL $1 :: $2"; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

# mkroot <dir> [devices...] — full healthy fake tree; devices default to all 64
# binder nodes. Loop state is healthy by default too (64 loop nodes,
# max_loop=64, grub-cmdline persistence); loop-specific scenarios live in
# the loop-device-guard suite.
mkroot() {
    local r="$1"; shift
    mkdir -p "$r/dev" "$r/modprobe.d" "$r/etc" "$r/binderfs" "$r/sys/module/binder_linux" \
             "$r/sys/module/loop/parameters"
    local devs=("$@")
    if [ ${#devs[@]} -eq 0 ]; then
        devs=("binder"); for i in $(seq 1 63); do devs+=("binder$i"); done
    fi
    for d in "${devs[@]}"; do touch "$r/dev/$d"; done
    local names="binder"; for i in $(seq 1 63); do names="$names,binder$i"; done
    printf 'options binder_linux devices=%s\n' "$names" > "$r/modprobe.d/binder.conf"
    printf 'binder_linux\n' > "$r/etc/modules"
    touch "$r/binderfs/binder-control"
    for i in $(seq 0 63); do touch "$r/dev/loop$i"; done
    printf '64\n' > "$r/sys/module/loop/parameters/max_loop"
    printf 'BOOT_IMAGE=/vmlinuz loop.max_loop=64 quiet\n' > "$r/cmdline"
    printf -v ENV_STR 'BV_DEVDIR=%s/dev BV_SYS_MODULES=%s/sys/module BV_MODPROBE_D=%s/modprobe.d BV_MODULES_FILE=%s/etc/modules BV_BINDERFS_DIR=%s/binderfs BV_PROC_CMDLINE=%s/cmdline' "$r" "$r" "$r" "$r" "$r" "$r"
}
runv() { # run verify-binder.sh in the current fake tree; returns output+rc
    # shellcheck disable=SC2086
    env $ENV_STR bash "$SCRIPT" ${1:+"$1"} >"$T/out" 2>&1
    echo $?
}

# ---------- T1: happy path ----------
mkroot "$T/r1"; rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 0 ] && ok "T1 happy exit 0" || bad "T1 happy exit 0" "rc=$rc"
echo "$out" | grep -q "VERDICT: PASS" && ok "T1 PASS verdict" || bad "T1 PASS verdict" "$out"
echo "$out" | grep -q "binder device nodes: 64/64" && ok "T1 64/64 count line" || bad "T1 64/64 count line" "$out"
echo "$out" | grep -q "^\[FAIL\]" && bad "T1 no FAIL lines" "$out" || ok "T1 no FAIL lines"

# ---------- T2: module dir absent -> WARN, still PASS ----------
mkroot "$T/r2"; rm -rf "$T/r2/sys/module/binder_linux"; rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 0 ] && echo "$out" | grep -q "WARN.*binder_linux not visible" \
    && ok "T2 module-absent WARN, still PASS" || bad "T2 module-absent" "rc=$rc :: $out"

# ---------- T3: 63 devices, binder37 missing -> PASS, missing named ----------
devs=("binder"); for i in $(seq 1 63); do [ "$i" != 37 ] && devs+=("binder$i"); done
mkroot "$T/r3" "${devs[@]}"; rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 0 ] && echo "$out" | grep -q "missing: binder37" \
    && ok "T3 63 nodes PASS, binder37 named" || bad "T3 63 nodes" "rc=$rc :: $out"

# ---------- T4: exactly 60 -> PASS (floor) ----------
devs=("binder"); for i in $(seq 1 59); do devs+=("binder$i"); done
mkroot "$T/r4" "${devs[@]}"; rc=$(runv)
[ "$rc" -eq 0 ] && ok "T4 exactly-60 PASS" || bad "T4 exactly-60 PASS" "rc=$rc"

# ---------- T5: 59 -> FAIL, remediation mentions modprobe ----------
devs=("binder"); for i in $(seq 1 58); do devs+=("binder$i"); done
mkroot "$T/r5" "${devs[@]}"; rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 1 ] && echo "$out" | grep -q "modprobe -r binder_linux" \
    && ok "T5 59 nodes FAIL + remediation" || bad "T5 59 nodes" "rc=$rc :: $out"

# ---------- T6: 0 devices -> FAIL ----------
mkroot "$T/r6" "unrelated"; rm -f "$T/r6/dev/unrelated"; rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 1 ] && echo "$out" | grep -q "only 0/64 binder nodes" \
    && ok "T6 0 nodes FAIL" || bad "T6 0 nodes" "rc=$rc :: $out"

# ---------- T7: numeric devices=64 regression -> FAIL, names the semantics ----------
mkroot "$T/r7"
printf 'options binder_linux devices=64\n' > "$T/r7/modprobe.d/binder.conf"
rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 1 ] && echo "$out" | grep -q "NAME list, not a count" \
    && ok "T7 numeric-devices FAIL names semantics" || bad "T7 numeric-devices" "rc=$rc :: $out"

# ---------- T8: 63-name list in conf -> FAIL naming expected 64 ----------
mkroot "$T/r8"
names="binder"; for i in $(seq 1 62); do names="$names,binder$i"; done
printf 'options binder_linux devices=%s\n' "$names" > "$T/r8/modprobe.d/binder.conf"
rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 1 ] && echo "$out" | grep -q "expected 64" \
    && ok "T8 short name-list FAIL" || bad "T8 short name-list" "rc=$rc :: $out"

# ---------- T9: binder.conf missing -> FAIL naming the path ----------
mkroot "$T/r9"; rm -f "$T/r9/modprobe.d/binder.conf"
rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 1 ] && echo "$out" | grep -q "binder.conf missing" \
    && ok "T9 missing conf FAIL" || bad "T9 missing conf" "rc=$rc :: $out"

# ---------- T10: /etc/modules without the line -> FAIL ----------
mkroot "$T/r10"; printf 'loop\n' > "$T/r10/etc/modules"
rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 1 ] && echo "$out" | grep -q "will not auto-load" \
    && ok "T10 modules-line FAIL" || bad "T10 modules-line" "rc=$rc :: $out"

# ---------- T11: binder-control missing -> WARN, still PASS ----------
mkroot "$T/r11"; rm -f "$T/r11/binderfs/binder-control"
rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 0 ] && echo "$out" | grep -q "binder-control missing" \
    && ok "T11 binder-control WARN" || bad "T11 binder-control" "rc=$rc :: $out"

# ---------- T12: non-char device node -> WARN, still counted ----------
mkroot "$T/r12"; rc=$(runv); out=$(cat "$T/out")  # regular files everywhere
echo "$out" | grep -q "not a character device" && echo "$out" | grep -q "64/64" \
    && ok "T12 non-char WARN + counted" || bad "T12 non-char" "$out"

# ---------- T13: remediation generator == install.sh generator (parity) ----------
sed -n '/# --- binder-names-begin ---/,/# --- binder-names-end ---/p' "$REPO/install.sh" \
    | grep -v '^#' > "$T/gen.sh"
[ -s "$T/gen.sh" ] || { bad "T13 install.sh generator block" "block empty"; }
bash -c 'source "$1"; printf "%s" "$BINDER_NAMES"' _ "$T/gen.sh" > "$T/install.names"
# the two remediation lines the script prints on failure
mkroot "$T/r13"; rm -f "$T/r13/modprobe.d/binder.conf"
runv >/dev/null; out=$(cat "$T/out")
genlines="$(echo "$out" | grep -E 'BINDER_NAMES=|echo "options binder_linux devices=')"
[ -n "$genlines" ] || { bad "T13 remediation generator lines" "not found in output"; }
scriptnames="$(bash -c 'eval "$1" >/dev/null 2>&1; printf "%s" "$BINDER_NAMES"' _ "$genlines")"
[ "$scriptnames" = "$(cat "$T/install.names")" ] \
    && [ "$(printf '%s\n' "$scriptnames" | tr ',' '\n' | wc -l)" -eq 64 ] \
    && ok "T13 generator parity (64 names)" \
    || bad "T13 generator parity" "script vs install.sh generator mismatch"

# ---------- T14: --help -> exit 0, usage ----------
# shellcheck disable=SC2086
env $ENV_STR bash "$SCRIPT" --help >"$T/out" 2>&1; rc=$?
[ "$rc" -eq 0 ] && grep -q "Usage:" "$T/out" && ok "T14 --help" || bad "T14 --help" "rc=$rc"

# ---------- T15: unknown arg -> exit 2 ----------
# shellcheck disable=SC2086
env $ENV_STR bash "$SCRIPT" --bogus >"$T/out" 2>&1; rc=$?
[ "$rc" -eq 2 ] && ok "T15 usage exit 2" || bad "T15 usage exit 2" "rc=$rc"

# ---------- T16: no secrets in output ----------
mkroot "$T/r16"
printf 'SECRET-PLANTED-TOKEN-xyz\n' >> "$T/r16/modprobe.d/binder.conf"
printf 'SECRET-PLANTED-TOKEN-xyz\n' >> "$T/r16/etc/modules"
rc=$(runv); out=$(cat "$T/out")
echo "$out" | grep -q "SECRET-PLANTED-TOKEN" \
    && bad "T16 no secrets" "token leaked" || ok "T16 no secrets in output"

# ---------- T17: env overrides honored (empty fake devdir, real /dev untouched) ----------
mkroot "$T/r17"; rm -f "$T/r17"/dev/binder*
rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 1 ] && echo "$out" | grep -q "only 0/64" \
    && ok "T17 BV_ overrides honored" || bad "T17 BV_ overrides" "rc=$rc :: $out"

# ---------- T18: whitespace-tolerant parsing ----------
mkroot "$T/r18"
names="binder"; for i in $(seq 1 63); do names="$names,binder$i"; done
printf 'options  binder_linux   devices = %s\n' "$names" > "$T/r18/modprobe.d/binder.conf"
rc=$(runv)
[ "$rc" -eq 0 ] && ok "T18 whitespace-tolerant" || bad "T18 whitespace-tolerant" "rc=$rc"

# ---------- T19: invalid name in list -> FAIL naming it ----------
mkroot "$T/r19"
names="binder"; for i in $(seq 1 62); do names="$names,binder$i"; done
printf 'options binder_linux devices=%s,foo\n' "$names" > "$T/r19/modprobe.d/binder.conf"
rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 1 ] && echo "$out" | grep -q "invalid names: foo" \
    && ok "T19 invalid name FAIL" || bad "T19 invalid name" "rc=$rc :: $out"

# ---------- T20: multiple options lines -> WARN ----------
mkroot "$T/r20"
names="binder"; for i in $(seq 1 63); do names="$names,binder$i"; done
printf 'options binder_linux devices=%s\noptions binder_linux devices=%s\n' "$names" "$names" \
    > "$T/r20/modprobe.d/binder.conf"
rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 0 ] && echo "$out" | grep -q "multiple 'options binder_linux' lines" \
    && ok "T20 multi-options WARN" || bad "T20 multi-options" "rc=$rc :: $out"

# ---------- T21: missing base 'binder' (63 numbered present) -> PASS, named ----------
devs=(); for i in $(seq 1 63); do devs+=("binder$i"); done
mkroot "$T/r21" "${devs[@]}"; rc=$(runv); out=$(cat "$T/out")
[ "$rc" -eq 0 ] && echo "$out" | grep -qF "missing: binder)" \
    && ok "T21 missing-base named" || bad "T21 missing-base" "rc=$rc :: $out"

# ---------- T22: read-only — tree untouched ----------
mkroot "$T/r22"
find "$T/r22" -type f | sort | xargs sha256sum > "$T/before"
runv >/dev/null
find "$T/r22" -type f | sort | xargs sha256sum > "$T/after"
cmp -s "$T/before" "$T/after" && ok "T22 read-only" || bad "T22 read-only" "tree changed"

# ---------- T23: script bash -n clean ----------
bash -n "$SCRIPT" && ok "T23 bash -n" || bad "T23 bash -n" "syntax"

# ---------- final ----------
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
