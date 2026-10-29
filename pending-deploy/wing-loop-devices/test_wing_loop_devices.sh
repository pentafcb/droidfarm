#!/bin/bash
# test_wing_loop_devices.sh — hermetic tests for wing/install-wing.sh's
# install_loop_pool() (verbatim-extracted between the wing-loop-devices
# markers) — the fresh-wing-node provisioning path.
#
# Everything runs against fake modprobe/mknod binaries and a fake
# /etc/modprobe.d + /etc/modules + /dev tree via the LP_* overrides; the
# real installer is never executed and nothing on this host is touched.
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

# --- fake modprobe/mknod: modprobe logs only; mknod touches the node file ---
cat > "$BIN/modprobe" <<EOF
#!/bin/bash
echo "modprobe \$*" >> "$T/calls"
exit 0
EOF
cat > "$BIN/mknod" <<EOF
#!/bin/bash
echo "mknod \$*" >> "$T/calls"
touch "\$1" 2>/dev/null || true
exit 0
EOF
chmod +x "$BIN/modprobe" "$BIN/mknod"
export PATH="$BIN:$PATH"

# fake report() (the installer reports stages to the dashboard)
report() { echo "REPORT $1 :: $2" >> "$T/calls"; }

# --- extract the function verbatim from the installer ---
BEGIN='# --- wing-loop-devices: install_loop_pool ---'
END='# --- /wing-loop-devices ---'
n_begin="$(grep -c -F -x -- "$BEGIN" "$REPO/wing/install-wing.sh")"
n_end="$(grep -c -F -x -- "$END" "$REPO/wing/install-wing.sh")"
if [ "$n_begin" = "1" ] && [ "$n_end" = "1" ]; then tpass; else tfail "delimiters present exactly once (begin=$n_begin end=$n_end)"; fi
FUNC="$(extract_block "$REPO/wing/install-wing.sh" "$BEGIN" "$END")"
if [ -n "$FUNC" ]; then tpass; else tfail "extracted block non-empty"; fi
echo "$FUNC" | bash -n && tpass || tfail "bash -n on extracted block"
eval "$FUNC"

mk_tree() { # fresh fake host tree; pre-create $2 loop nodes
    IROOT="$T/iroot"
    rm -rf "$IROOT"; mkdir -p "$IROOT/modprobe.d" "$IROOT/dev"
    : > "$IROOT/modules"
    for i in $(seq 0 $((${2:-0} - 1)) 2>/dev/null); do : > "$IROOT/dev/loop$i"; done
    export LP_MODPROBE_D="$IROOT/modprobe.d"
    export LP_MODULES_FILE="$IROOT/modules"
    export LP_DEV_DIR="$IROOT/dev"
    : > "$T/calls"
}

# --- 1. happy path: 8 nodes pre-existing ---
mk_tree x 8
out="$(install_loop_pool 2>&1)"
[ -f "$IROOT/modprobe.d/loop.conf" ] && grep -qx "options loop max_loop=64" "$IROOT/modprobe.d/loop.conf" && tpass || tfail "loop.conf carries options loop max_loop=64"
[ "$(grep -c -x "options loop max_loop=64" "$IROOT/modprobe.d/loop.conf")" = "1" ] && tpass || tfail "loop.conf line written exactly once"
grep -qx "loop" "$IROOT/modules" && tpass || tfail "/etc/modules gains the loop line"
[ "$(grep -c -x "loop" "$IROOT/modules")" = "1" ] && tpass || tfail "modules line written exactly once"
grep -q "^modprobe loop max_loop=64$" "$T/calls" && tpass || tfail "modprobe loop max_loop=64 issued"
[ "$(grep -c "^mknod " "$T/calls")" = "56" ] && tpass || tfail "mknod only for the 56 missing nodes (got $(grep -c '^mknod ' "$T/calls"))"
grep -q "REPORT devices :: loop device pool ready (64/64)" "$T/calls" && tpass || tfail "report ready (64/64)"

# --- 2. idempotent re-run: nothing duplicated, no new mknods ---
: > "$T/calls"
out="$(install_loop_pool 2>&1)"
[ "$(grep -c -x "options loop max_loop=64" "$IROOT/modprobe.d/loop.conf")" = "1" ] && tpass || tfail "re-run: loop.conf not duplicated"
[ "$(grep -c -x "loop" "$IROOT/modules")" = "1" ] && tpass || tfail "re-run: modules line not duplicated"
[ "$(grep -c "^mknod " "$T/calls")" = "0" ] && tpass || tfail "re-run: no mknod when all 64 exist"

# --- 3. short pool: mknod fails -> WARNING ---
cat > "$BIN/mknod" <<EOF
#!/bin/bash
echo "mknod \$*" >> "$T/calls"
exit 1
EOF
chmod +x "$BIN/mknod"
mk_tree x 8
out="$(install_loop_pool 2>&1)"
grep -q "REPORT devices :: WARNING: only 8/64 loop devices present" "$T/calls" && tpass || tfail "short pool reports WARNING with count"

# --- 4. no apostrophes in the block (would break the outer single-quoted installer) ---
case "$FUNC" in
    *"'"*) tfail "apostrophe inside the single-quoted installer block" ;;
    *) tpass ;;
esac

# --- 5. real paths only reachable through LP_* overrides ---
badpath=0
while IFS= read -r line; do
    case "$line" in
        *"/etc/"*|*"/dev/loop"*)
            case "$line" in
                *"LP_"*) ;;
                *) badpath=1; echo "  hardcoded real path: $line" ;;
            esac
            ;;
    esac
done <<EOF
$FUNC
EOF
[ "$badpath" = "0" ] && tpass || tfail "block touches real paths outside LP_* overrides"

echo ""
echo "$TPASS passed, $TFAIL failed"
[ "$TFAIL" = "0" ]
