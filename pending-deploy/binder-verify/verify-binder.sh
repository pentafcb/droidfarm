#!/bin/bash
# verify-binder.sh — read-only binder stack verification for the Jerusalem fleet host.
#
# Run as root on the VPS (e.g. 64.177.49.68) BEFORE creating instances:
#   bash verify-binder.sh
#
# Verifies the whole binder chain the fleet depends on:
#   1. binder_linux kernel module loaded
#   2. /dev/binder + /dev/binder1..63 named device nodes present (>=60 PASS floor)
#   3. /dev/binderfs/binder-control usable (the manager's instance-create
#      self-heal needs it)
#   4. persistent config so the pool survives reboot:
#      /etc/modprobe.d/binder.conf carries the 64-name list, /etc/modules
#      loads binder_linux on boot
#
# And the loop-device pool Waydroid boots need (~2 per instance; the
# 2026-10-08 fleet recovery needed ~40 for 20):
#   5. loop kernel module loaded
#   6. /dev/loop0..loop63 block nodes present (>=60 PASS floor)
#   7. the live driver's max_loop is >= 60 (nodes beyond it are unusable —
#      mknod cannot grow the driver's table)
#   8. persistent config so the pool survives reboot: EITHER
#      `loop.max_loop=64` on the kernel cmdline (GRUB path) OR
#      /etc/modprobe.d/loop.conf with `options loop max_loop=64` plus
#      `loop` in /etc/modules
#
# Also fails closed on the 2026-10-13 regression: a NUMERIC `devices=64` value
# in binder.conf (the module parameter is a comma-separated NAME list, not a
# count — a numeric value creates one device literally named "64" and zero
# /dev/binder* nodes).
#
# Exit: 0 PASS, 1 FAIL (remediation printed), 2 usage error.
# Read-only: never writes, never touches instance data/volumes/containers.
# Never prints file contents beyond config lines it explicitly validates.
#
# Env overrides for hermetic testing (defaults are the real paths):
#   BV_DEVDIR BV_SYS_MODULES BV_MODPROBE_D BV_MODULES_FILE BV_BINDERFS_DIR
#   BV_PROC_CMDLINE
set -u

DEVDIR="${BV_DEVDIR:-/dev}"
SYSMOD="${BV_SYS_MODULES:-/sys/module}"
MODPROBE_D="${BV_MODPROBE_D:-/etc/modprobe.d}"
MODULES_FILE="${BV_MODULES_FILE:-/etc/modules}"
BINDERFS_DIR="${BV_BINDERFS_DIR:-/dev/binderfs}"
PROC_CMDLINE="${BV_PROC_CMDLINE:-/proc/cmdline}"

EXPECTED=64        # binder + binder1..binder63 (install.sh generator)
PASS_FLOOR=60      # install.sh warns below this; fail closed under it
LOOP_EXPECTED=64   # loop0..loop63 (install.sh step 4)
LOOP_FLOOR=60      # fail closed under this

usage() {
    cat <<EOF
Usage: bash verify-binder.sh [--help]

Read-only verification of the kernel binder stack on a fleet host.
Exit 0 = PASS (ready for instance creation), 1 = FAIL (remediation printed),
2 = usage error.
EOF
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    exit 0
fi
if [ -n "${1:-}" ]; then
    usage >&2
    exit 2
fi

ok=0; fail=0; warn=0
say()  { printf '%s\n' "$1"; }
tag()  { printf '[%s] %s\n' "$1" "$2"; }

# --- 1. kernel module loaded ---
if [ -d "$SYSMOD/binder_linux" ]; then
    tag OK "binder_linux module loaded"
else
    tag WARN "binder_linux not visible in $SYSMOD (may be built-in or under another path)"
    warn=$((warn+1))
fi

# --- 2. named device nodes ---
count=0
missing=()
nonchar=()
for i in $(seq 0 63); do
    if [ "$i" -eq 0 ]; then n="binder"; else n="binder$i"; fi
    if [ -e "$DEVDIR/$n" ]; then
        count=$((count+1))
        [ -c "$DEVDIR/$n" ] || nonchar+=("$n")
    else
        missing+=("$n")
    fi
done
tag INFO "binder device nodes: $count/$EXPECTED"
for n in ${nonchar[@]+"${nonchar[@]}"}; do
    tag WARN "$n exists but is not a character device"
    warn=$((warn+1))
done
if [ "$count" -ge "$PASS_FLOOR" ]; then
    if [ "$count" -lt "$EXPECTED" ]; then
        tag WARN "below $EXPECTED nodes (missing: ${missing[*]}); still >= $PASS_FLOOR, creation can proceed"
        warn=$((warn+1))
    else
        tag OK "all $EXPECTED named binder nodes present"
    fi
else
    tag FAIL "only $count/$EXPECTED binder nodes (missing: ${missing[*]})"
    fail=$((fail+1))
fi

# --- 3. binderfs mount usable (manager self-heal path) ---
if [ -e "$BINDERFS_DIR/binder-control" ]; then
    tag OK "binder-control present (manager binderfs self-heal usable)"
else
    tag WARN "binder-control missing — manager's create-time self-heal needs it (modprobe binder_linux)"
    warn=$((warn+1))
fi

# --- 4. persistent config ---
CONF="$MODPROBE_D/binder.conf"
numeric_re="^options[[:space:]]+binder_linux[[:space:]]+devices[[:space:]]*=[[:space:]]*[0-9]+([[:space:],]|\$)"
if [ -f "$CONF" ]; then
    # 4a. the 2026-10-13 regression: numeric devices= value
    if grep -Eq "$numeric_re" "$CONF"; then
        tag FAIL "binder.conf has a NUMERIC devices= value — the binder parameter is a comma-separated NAME list, not a count (this creates one device named \"64\" and zero /dev/binder* nodes)"
        fail=$((fail+1))
    else
        line="$(grep -E '^options[[:space:]]+binder_linux[[:space:]]+devices[[:space:]]*=' "$CONF" | head -1)"
        if [ -n "$line" ]; then
            val="$(printf '%s' "$line" | sed -E 's/^options[[:space:]]+binder_linux[[:space:]]+devices[[:space:]]*=[[:space:]]*//')"
            # count comma-separated entries, validate names
            nvals=0; bad=()
            IFS=',' read -ra PARTS <<< "$val"
            for p in "${PARTS[@]}"; do
                p="$(printf '%s' "$p" | tr -d '[:space:]')"
                [ -z "$p" ] && continue
                nvals=$((nvals+1))
                case "$p" in
                    binder|binder[0-9]*) ;;
                    *) bad+=("$p");;
                esac
            done
            if [ ${#bad[@]} -gt 0 ]; then
                tag FAIL "binder.conf devices= has invalid names: ${bad[*]}"
                fail=$((fail+1))
            elif [ "$nvals" -ne "$EXPECTED" ]; then
                tag FAIL "binder.conf devices= names $nvals devices (expected $EXPECTED: binder,binder1..binder63)"
                fail=$((fail+1))
            else
                tag OK "binder.conf carries the persistent 64-name list"
            fi
            # ambiguous: more than one options line for binder_linux
            if [ "$(grep -cE '^options[[:space:]]+binder_linux' "$CONF")" -gt 1 ]; then
                tag WARN "multiple 'options binder_linux' lines in binder.conf — last one wins, clean it up"
                warn=$((warn+1))
            fi
        else
            tag FAIL "binder.conf exists but has no 'options binder_linux devices=...' line"
            fail=$((fail+1))
        fi
    fi
else
    tag FAIL "$CONF missing — the 64-device pool will not survive a reboot"
    fail=$((fail+1))
fi

# /etc/modules: load binder_linux at boot
if grep -qx 'binder_linux' "$MODULES_FILE" 2>/dev/null; then
    tag OK "/etc/modules loads binder_linux on boot"
else
    tag FAIL "$MODULES_FILE lacks a 'binder_linux' line — the module will not auto-load after reboot"
    fail=$((fail+1))
fi

# --- 5. loop kernel module loaded ---
if [ -d "$SYSMOD/loop" ]; then
    tag OK "loop module loaded"
else
    tag WARN "loop not visible in $SYSMOD (may be built-in)"
    warn=$((warn+1))
fi

# --- 6. loop device nodes ---
lcount=0
lmissing=()
lnonblock=()
for i in $(seq 0 63); do
    if [ -e "$DEVDIR/loop$i" ]; then
        lcount=$((lcount+1))
        [ -b "$DEVDIR/loop$i" ] || lnonblock+=("loop$i")
    else
        lmissing+=("loop$i")
    fi
done
tag INFO "loop device nodes: $lcount/$LOOP_EXPECTED"
for n in ${lnonblock[@]+"${lnonblock[@]}"}; do
    tag WARN "$n exists but is not a block device"
    warn=$((warn+1))
done
if [ "$lcount" -ge "$LOOP_FLOOR" ]; then
    if [ "$lcount" -lt "$LOOP_EXPECTED" ]; then
        tag WARN "below $LOOP_EXPECTED nodes (missing: ${lmissing[*]}); still >= $LOOP_FLOOR"
        warn=$((warn+1))
    else
        tag OK "all $LOOP_EXPECTED loop nodes present"
    fi
else
    tag FAIL "only $lcount/$LOOP_EXPECTED loop nodes (missing: ${lmissing[*]}) — Waydroid needs ~2 per instance at boot"
    fail=$((fail+1))
fi

# --- 7. driver capacity: max_loop below the floor makes extra nodes useless ---
MAXLOOP_FILE="$SYSMOD/loop/parameters/max_loop"
if [ -f "$MAXLOOP_FILE" ]; then
    maxloop="$(tr -cd '0-9' < "$MAXLOOP_FILE")"
    if [ -n "$maxloop" ]; then
        if [ "$maxloop" -ge "$LOOP_FLOOR" ]; then
            tag OK "loop max_loop=$maxloop (driver can serve the pool)"
        else
            tag FAIL "loop max_loop=$maxloop < $LOOP_FLOOR — mknod nodes beyond it are unusable; reload the module with max_loop=64 or reboot with persistent config"
            fail=$((fail+1))
        fi
    else
        tag WARN "could not parse $MAXLOOP_FILE — node count is the signal"
        warn=$((warn+1))
    fi
else
    tag WARN "max_loop not visible (built-in driver?) — node count is the signal"
    warn=$((warn+1))
fi

# --- 8. loop persistent config (survives reboot) ---
# Persistent via EITHER the kernel cmdline (GRUB path: loop.max_loop=N)
# OR modprobe.d (options loop max_loop=N) + auto-load (/etc/modules).
loop_persistent=""
cmdline="$(cat "$PROC_CMDLINE" 2>/dev/null || true)"
case " $cmdline " in
    *" loop.max_loop="*)
        cval="$(printf '%s' "$cmdline" | sed -E 's/.* loop\.max_loop=([0-9]+).*/\1/')"
        if [ -n "$cval" ] && [ "$cval" -ge "$LOOP_FLOOR" ]; then
            loop_persistent="kernel cmdline (loop.max_loop=$cval)"
        fi
        ;;
esac
if [ -z "$loop_persistent" ]; then
    LCONF="$MODPROBE_D/loop.conf"
    if [ -f "$LCONF" ]; then
        lline="$(grep -E '^options[[:space:]]+loop[[:space:]]+max_loop[[:space:]]*=' "$LCONF" | head -1)"
        if [ -n "$lline" ]; then
            lval="$(printf '%s' "$lline" | sed -E 's/^options[[:space:]]+loop[[:space:]]+max_loop[[:space:]]*=[[:space:]]*//' | tr -cd '0-9')"
            if [ -n "$lval" ] && [ "$lval" -ge "$LOOP_FLOOR" ]; then
                # The conf only takes effect when the module (re)loads: without
                # an auto-load line a reboot can still come up with the stock
                # pool, so the missing line fails honestly (a currently-loaded
                # module does not prove it will load after reboot).
                if grep -qx 'loop' "$MODULES_FILE" 2>/dev/null; then
                    loop_persistent="modprobe.d (options loop max_loop=$lval)"
                else
                    tag FAIL "$LCONF sets max_loop=$lval but the module will not auto-load after reboot ($MODULES_FILE lacks a 'loop' line)"
                    fail=$((fail+1))
                fi
            else
                tag FAIL "$LCONF has max_loop below $LOOP_FLOOR (or unparsable): $lline"
                fail=$((fail+1))
            fi
        else
            tag FAIL "$LCONF exists but has no 'options loop max_loop=...' line"
            fail=$((fail+1))
        fi
    fi
fi
if [ -n "$loop_persistent" ]; then
    tag OK "loop pool persistent via $loop_persistent"
elif [ ! -f "$MODPROBE_D/loop.conf" ]; then
    # The kernel cmdline was already checked above; a wrong-but-present
    # loop.conf already failed specifically. This is the missing-config case.
    tag FAIL "no persistent loop config — neither loop.max_loop>=${LOOP_FLOOR} on the kernel cmdline nor $MODPROBE_D/loop.conf; the pool drops to the stock 8 on reboot"
    fail=$((fail+1))
fi

# --- verdict ---
say ""
if [ "$fail" -eq 0 ]; then
    tag INFO "VERDICT: PASS ($warn warning(s)) — host is ready for instance creation"
    exit 0
fi

tag INFO "VERDICT: FAIL ($fail failure(s), $warn warning(s))"
say ""
say "Remediation (as root):"
say "  Binder pool:"
say "     BINDER_NAMES=\"binder\"; for i in \$(seq 1 63); do BINDER_NAMES=\"\$BINDER_NAMES,binder\$i\"; done"
say "     echo \"options binder_linux devices=\$BINDER_NAMES\" > $CONF"
say "     grep -qx 'binder_linux' $MODULES_FILE || echo 'binder_linux' >> $MODULES_FILE"
say "     Reload the module (or reboot): modprobe -r binder_linux && modprobe binder_linux"
say "  Loop pool:"
say "     echo 'options loop max_loop=64' > $MODPROBE_D/loop.conf"
say "     grep -qx 'loop' $MODULES_FILE || echo 'loop' >> $MODULES_FILE"
say "     modprobe loop max_loop=64   # live module keeps its old max_loop — reboot if it was already loaded with < 60"
say "     for i in \$(seq 0 63); do [ -e /dev/loop\$i ] || mknod /dev/loop\$i b 7 \$i; done"
say "  3. Re-run this script — expect 64/64 binder nodes and 64/64 loop nodes."
say "  Note: the manager also self-heals the binder pool via binderfs and the"
say "  loop pool via modprobe/mknod at instance-create time (best-effort;"
say "  neither can fix a live module loaded with too-small parameters)."
exit 1
