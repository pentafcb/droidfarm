#!/bin/bash
# test_loop_device_guard.sh — hermetic tests for the loop-device-guard package:
#   1. install.sh's install_loop_pool() (verbatim-extracted between the
#      loop-persist markers) — the fresh-install path.
#   2. verify-binder.sh sections 5-8 (loop chain) against fake host roots.
#
# Everything runs against fake modprobe/mknod binaries and fake /dev,
# /sys/module, /etc/modprobe.d, /etc/modules, /proc/cmdline trees; the real
# scripts are never executed against real paths and nothing on this host is
# touched. (mknod is not permitted in this container, so fixtures use regular
# files — the script's "not a block device" WARN is expected and tolerated.)
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TPASS=0; TFAIL=0
tpass() { TPASS=$((TPASS+1)); }
tfail() { TFAIL=$((TFAIL+1)); echo "TEST-FAIL: $*"; }

extract_block() { # file begin-marker end-marker
    awk -v b="$2" -v e="$3" '$0==b{f=1;next} $0==e{f=0} f' "$1"
}

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"
mkdir -p "$BIN"
: > "$T/calls"

# --- fake modprobe/mknod (log calls, mknod touches nothing) ---
cat > "$BIN/modprobe" <<EOF
#!/bin/bash
echo "modprobe \$*" >> "$T/calls"
exit 0
EOF
cat > "$BIN/mknod" <<EOF
#!/bin/bash
echo "mknod \$*" >> "$T/calls"
exit 0
EOF
chmod +x "$BIN/modprobe" "$BIN/mknod"
export PATH="$BIN:$PATH"

# --- fake host tree for install_loop_pool ---
mk_install_tree() { # -> sets IROOT
    IROOT="$T/iroot"
    rm -rf "$IROOT"; mkdir -p "$IROOT/modprobe.d" "$IROOT/dev"
    : > "$IROOT/modules"
    export LOOP_MODPROBE_D="$IROOT/modprobe.d"
    export LOOP_MODULES_FILE="$IROOT/modules"
    export LOOP_DEVDIR="$IROOT/dev"
}

# ================= 1. install_loop_pool() =================
FUNC="$(extract_block "$REPO/install.sh" '# --- loop-persist-begin ---' '# --- loop-persist-end ---')"
[ -n "$FUNC" ] || { echo "TEST-FAIL: loop-persist block not found in install.sh"; }
eval "$FUNC"

mk_install_tree
out="$(install_loop_pool 2>&1)"
tpass_msg() { tpass; }
[ -f "$IROOT/modprobe.d/loop.conf" ] \
    && grep -qx 'options loop max_loop=64' "$IROOT/modprobe.d/loop.conf" \
    && tpass || tfail "L1 loop.conf written with max_loop=64"
grep -qx '^loop$' "$IROOT/modules" && tpass || tfail "L2 modules gains 'loop' line"
[ "$(grep -c '^loop$' "$IROOT/modules")" = "1" ] && tpass || tfail "L2b exactly one loop line"
install_loop_pool >/dev/null 2>&1
[ "$(grep -c '^loop$' "$IROOT/modules")" = "1" ] && tpass || tfail "L3 idempotent: still one loop line"
grep -q '^modprobe loop max_loop=64$' "$T/calls" && tpass || tfail "L4 modprobe loop max_loop=64 called"
# missing nodes -> mknod attempted for loop0..63 (dev dir empty)
: > "$T/calls"
install_loop_pool >/dev/null 2>&1
n_mknod="$(grep -c '^mknod .*/loop[0-9]* b 7' "$T/calls")"
[ "$n_mknod" = "64" ] && tpass || tfail "L5 mknod for all 64 missing nodes (got $n_mknod)"
# existing nodes are not re-created
touch "$IROOT/dev/loop0" "$IROOT/dev/loop1"
: > "$T/calls"
install_loop_pool >/dev/null 2>&1
n_mknod="$(grep -c '^mknod .*/loop[0-9]* b 7' "$T/calls")"
[ "$n_mknod" = "62" ] && tpass || tfail "L6 present nodes skipped (got $n_mknod, want 62)"
# verify count + WARN below 60
echo "$out" | grep -q 'Loop devices available:' && tpass || tfail "L7 verify count printed"
echo "$out" | grep -q 'WARNING: fewer than 60 loop nodes' && tpass || tfail "L8 WARN below 60"
# 60 nodes -> no warning
for i in $(seq 0 59); do touch "$IROOT/dev/loop$i"; done
out="$(install_loop_pool 2>&1)"
echo "$out" | grep -q 'Loop devices available: 60' && tpass || tfail "L9 count 60 reported"
echo "$out" | grep -q 'WARNING' && tfail "L10 no warning at 60" || tpass
# function uses only overridden paths (no real /etc or /dev writes)
if [ ! -e /etc/modprobe.d/loop.conf ]; then tpass; else tfail "L11 real /etc/modprobe.d untouched"; fi
bash -n "$REPO/install.sh" && tpass || tfail "L12 install.sh bash -n"

# ================= 2. verify-binder.sh loop sections =================
VB="$REPO/pending-deploy/binder-verify/verify-binder.sh"
bash -n "$VB" && tpass || tfail "V0 verify-binder.sh bash -n"

mk_vtree() { # loop_nodes maxloop module(0/1) persist-mode
    # persist-mode: grub | modprobed | none | wrongconf | nomodulesline
    local nodes="$1" maxloop="$2" mod="$3" pmode="$4"
    VROOT="$T/vroot"
    rm -rf "$VROOT"
    mkdir -p "$VROOT/dev" "$VROOT/sys/module" "$VROOT/modprobe.d"
    : > "$VROOT/modules"; : > "$VROOT/cmdline"
    local i
    for i in $(seq 0 $((nodes - 1))); do : > "$VROOT/dev/loop$i"; done
    if [ "$mod" = "1" ]; then
        mkdir -p "$VROOT/sys/module/loop/parameters"
        echo "$maxloop" > "$VROOT/sys/module/loop/parameters/max_loop"
    fi
    case "$pmode" in
        grub) echo "BOOT_IMAGE=/vmlinuz root=/dev/sda1 loop.max_loop=64 quiet" > "$VROOT/cmdline";;
        modprobed)
            echo "options loop max_loop=64" > "$VROOT/modprobe.d/loop.conf"
            echo "loop" >> "$VROOT/modules";;
        wrongconf)
            echo "options loop max_loop=8" > "$VROOT/modprobe.d/loop.conf"
            echo "loop" >> "$VROOT/modules";;
        nomodulesline)
            echo "options loop max_loop=64" > "$VROOT/modprobe.d/loop.conf";;
    esac
    # binder side kept healthy so loop is the only signal
    for i in $(seq 0 63); do
        if [ "$i" -eq 0 ]; then : > "$VROOT/dev/binder"; else : > "$VROOT/dev/binder$i"; fi
    done
    mkdir -p "$VROOT/sys/module/binder_linux" "$VROOT/binderfs"
    : > "$VROOT/binderfs/binder-control"
    bn="binder"; for i in $(seq 1 63); do bn="$bn,binder$i"; done
    echo "options binder_linux devices=$bn" > "$VROOT/modprobe.d/binder.conf"
    echo "binder_linux" >> "$VROOT/modules"
    export BV_DEVDIR="$VROOT/dev" BV_SYS_MODULES="$VROOT/sys/module" \
           BV_MODPROBE_D="$VROOT/modprobe.d" BV_MODULES_FILE="$VROOT/modules" \
           BV_BINDERFS_DIR="$VROOT/binderfs" BV_PROC_CMDLINE="$VROOT/cmdline"
}
run_vb() { bash "$VB" 2>&1; }

# happy path: 64 loop nodes, max_loop=64, grub persistence
mk_vtree 64 64 1 grub
out="$(run_vb)"; rc=$?
[ "$rc" = "0" ] && tpass || tfail "V1 happy path exit 0 (got $rc)"
echo "$out" | grep -q 'VERDICT: PASS' && tpass || tfail "V2 happy path VERDICT: PASS"
echo "$out" | grep -q 'loop pool persistent via kernel cmdline' && tpass || tfail "V3 grub persistence recognized"

# modprobe.d + modules persistence also passes
mk_vtree 64 64 1 modprobed
out="$(run_vb)"; rc=$?
[ "$rc" = "0" ] && echo "$out" | grep -q 'loop pool persistent via modprobe.d' \
    && tpass || tfail "V4 modprobe.d persistence exit 0 + recognized"

# 59 nodes -> FAIL
mk_vtree 59 64 1 grub
out="$(run_vb)"; rc=$?
[ "$rc" = "1" ] && echo "$out" | grep -q 'only 59/64 loop nodes' \
    && tpass || tfail "V5 59 nodes -> FAIL with count"

# 60 nodes -> PASS (>= floor)
mk_vtree 60 64 1 grub
out="$(run_vb)"; rc=$?
[ "$rc" = "0" ] && tpass || tfail "V6 60 nodes -> PASS (floor)"

# 0 nodes -> FAIL
mk_vtree 0 64 1 grub
out="$(run_vb)"; rc=$?
[ "$rc" = "1" ] && tpass || tfail "V7 0 nodes -> FAIL"

# max_loop=8 with 64 nodes -> FAIL (capacity)
mk_vtree 64 8 1 grub
out="$(run_vb)"; rc=$?
[ "$rc" = "1" ] && echo "$out" | grep -q 'max_loop=8 < 60' \
    && tpass || tfail "V8 max_loop=8 -> FAIL capacity"

# no persistence anywhere -> FAIL
mk_vtree 64 64 1 none
out="$(run_vb)"; rc=$?
[ "$rc" = "1" ] && echo "$out" | grep -q 'no persistent loop config' \
    && tpass || tfail "V9 missing persistence -> FAIL"

# wrong max_loop in loop.conf -> FAIL
mk_vtree 64 64 1 wrongconf
out="$(run_vb)"; rc=$?
[ "$rc" = "1" ] && echo "$out" | grep -q 'max_loop below 60' \
    && tpass || tfail "V10 wrong loop.conf -> FAIL"

# loop.conf without modules line -> FAIL
mk_vtree 64 64 1 nomodulesline
out="$(run_vb)"; rc=$?
[ "$rc" = "1" ] && echo "$out" | grep -q "lacks a 'loop' line" \
    && tpass || tfail "V11 modules line missing -> FAIL"

# remediation mentions the loop fix
mk_vtree 0 64 1 none
out="$(run_vb)"
echo "$out" | grep -q 'options loop max_loop=64' && tpass || tfail "V12 remediation names loop.conf fix"

# read-only: tree untouched by a run
mk_vtree 64 64 1 grub
before="$(find "$VROOT" | sort | md5sum)"
run_vb >/dev/null 2>&1
after="$(find "$VROOT" | sort | md5sum)"
[ "$before" = "$after" ] && tpass || tfail "V13 read-only: tree unchanged"

# --help / usage
bash "$VB" --help >/dev/null 2>&1; [ "$?" = "0" ] && tpass || tfail "V14 --help exit 0"
bash "$VB" --bogus >/dev/null 2>&1; [ "$?" = "2" ] && tpass || tfail "V15 bad arg exit 2"

# module not visible -> WARN, still PASS when the rest is healthy
mk_vtree 64 64 0 grub
out="$(run_vb)"; rc=$?
[ "$rc" = "0" ] && echo "$out" | grep -q 'loop not visible' \
    && tpass || tfail "V16 module-absent WARN, still PASS"

echo ""
echo "$TPASS passed, $TFAIL failed"
[ "$TFAIL" = "0" ]
