#!/bin/bash
# push-optimizer.sh — push the canonical android-optimize.sh into every booted
# local instance's data volume and run it once inside Android via waydroid shell.
#
# Run ON THE VPS as root:
#   bash /root/jerusalem/push-optimizer.sh [/root/jerusalem/android-optimize.sh]
#
# SAFE: only replaces the optimizer script file inside each instance's data
# volume and runs the verified working-set commands (pm disable-user,
# svc stayon, dumpsys deviceidle disable, pm trim-caches). Idempotent:
# re-running changes nothing. Never touches instance data, credentials,
# tokens, or any other helper. Containers that are not booted yet are skipped
# (the entrypoint applies the optimizer at every boot, so they get it anyway).
# If a run fails, re-running this script is always safe.
#
# The per-instance waydroid run is already bounded by OPT_TIMEOUT (default
# 60s). The container listing goes through dex() (bounded by DEX_TIMEOUT,
# default 30s) so a wedged daemon fails closed instead of hanging the whole
# push. Per-instance [i/N] progress lines show the operator exactly where a
# hang was.
#
# Env overrides (also used by the unit tests):
#   VOLBASE       docker volumes base (default /var/lib/docker/volumes)
#   OPT_TIMEOUT   seconds per-container waydroid run (default 60)
#   DEX_TIMEOUT   seconds for the docker ps listing (default 30)
set -u

SRC="${1:-/root/jerusalem/android-optimize.sh}"
VOLBASE="${VOLBASE:-/var/lib/docker/volumes}"
OPT_TIMEOUT="${OPT_TIMEOUT:-60}"

# --- bounded docker client (listing only; the waydroid run has its own bound)
DEX_TIMEOUT="${DEX_TIMEOUT:-30}"
case "$DEX_TIMEOUT" in ''|*[!0-9]*) DEX_TIMEOUT=30;; esac
_HAVE_TIMEOUT=""
command -v timeout >/dev/null 2>&1 && _HAVE_TIMEOUT=1
dex() {
    if [ -n "$_HAVE_TIMEOUT" ]; then
        timeout "$DEX_TIMEOUT" docker "$@"
    else
        docker "$@"
    fi
}
# ---------------------------------------------------------------------------

[ -f "$SRC" ] || { echo "FAIL: source missing: $SRC"; exit 1; }
bash -n "$SRC" || { echo "FAIL: syntax check failed: $SRC"; exit 1; }

# Fail closed when the daemon itself is wedged: an empty instance list with
# exit 0 would silently "succeed" while pushing nothing.
ps_out=$(dex ps --format '{{.Names}}' 2>/dev/null); ps_rc=$?
if [ "$ps_rc" -ne 0 ]; then
    echo "FAIL: docker ps failed (rc=$ps_rc) - docker daemon may be wedged"
    exit 1
fi
mapfile -t _CS < <(printf '%s\n' "$ps_out" | grep '^wd-' | sort)
total=${#_CS[@]}

fail=0
done_n=0
skip_n=0
i=0
if [ "$total" -gt 0 ]; then
for c in "${_CS[@]}"; do
    i=$((i + 1))
    dfdir="$VOLBASE/$c-data/_data/data/media/0/jerusalem"
    if [ ! -f "$dfdir/booted" ]; then
        echo "[$i/$total] $c: not booted yet, skipping (entrypoint applies the optimizer at boot)"
        skip_n=$((skip_n + 1))
        continue
    fi
    if ! cp -f "$SRC" "$dfdir/android-optimize.sh"; then
        echo "[$i/$total] $c: copy FAILED"
        fail=1
        continue
    fi
    chmod +x "$dfdir/android-optimize.sh"
    # Lines already in optimize.log before this run — the completion marker
    # must appear AFTER them, so a stale marker from an old run can't fake
    # a successful push.
    before=0
    [ -f "$dfdir/optimize.log" ] && before=$(wc -l < "$dfdir/optimize.log")
    if ! timeout "$OPT_TIMEOUT" docker exec "$c" waydroid shell sh /data/media/0/jerusalem/android-optimize.sh >/dev/null 2>&1; then
        echo "[$i/$total] $c: optimizer run FAILED"
        fail=1
        continue
    fi
    if [ -f "$dfdir/optimize.log" ] \
        && tail -n +"$((before + 1))" "$dfdir/optimize.log" 2>/dev/null | grep -q '\[optimize\] done'; then
        echo "[$i/$total] $c: optimizer applied"
        done_n=$((done_n + 1))
    else
        echo "[$i/$total] $c: optimizer ran but optimize.log shows no new completion marker"
        fail=1
    fi
done
fi

echo "applied to $done_n instance(s), skipped $skip_n"
exit $fail
