#!/bin/bash
# push-keepalive.sh — push the canonical roblox-keepalive.sh v2 into every
# running local instance's data volume and restart its keepalive loop.
#
# Run ON THE MAIN VPS as root:
#   bash /root/jerusalem-deploy/push-keepalive.sh [/root/jerusalem/roblox-keepalive.sh]
#
# SAFE: only replaces the keepalive script file inside each instance's data
# volume and restarts that one loop. Never touches instance data,
# credentials, tokens, or any other helper. If a loop fails to restart, the
# host watchdog (df-watchdog.sh, minute cron) restarts the dead helper
# itself — the push is self-healing.
#
# Every docker call goes through dex() (bounded by DEX_TIMEOUT, default 30s,
# env-overridable; garbage -> 30; bare docker when the timeout binary is
# missing) so one wedged container or daemon fails that instance instead of
# hanging the whole push. Per-instance [i/N] progress lines show the
# operator exactly where a hang was.
set -u

SRC="${1:-/root/jerusalem/roblox-keepalive.sh}"
VOLBASE="${VOLBASE:-/var/lib/docker/volumes}"

# --- bounded docker client ---------------------------------------------------
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
i=0
if [ "$total" -gt 0 ]; then
for c in "${_CS[@]}"; do
    i=$((i + 1))
    dfdir="$VOLBASE/$c-data/_data/data/media/0/jerusalem"
    if [ ! -f "$dfdir/booted" ]; then
        echo "[$i/$total] $c: not booted yet, skipping"
        continue
    fi
    if ! cp -f "$SRC" "$dfdir/roblox-keepalive.sh"; then
        echo "[$i/$total] $c: copy FAILED"
        fail=1
        continue
    fi
    chmod +x "$dfdir/roblox-keepalive.sh"
    # Restart the loop so the new script takes effect now (the old loop is
    # pkill'd; the watchdog would also restart it within a minute).
    dex exec "$c" pkill -f roblox-keepalive.sh 2>/dev/null
    dex exec "$c" nohup waydroid shell sh /data/media/0/jerusalem/roblox-keepalive.sh >/dev/null 2>&1 &
    sleep 2
    if dex exec "$c" pgrep -f roblox-keepalive.sh >/dev/null 2>&1; then
        echo "[$i/$total] $c: keepalive v2 running"
        done_n=$((done_n + 1))
    else
        echo "[$i/$total] $c: keepalive restart FAILED (watchdog will retry within a minute)"
        fail=1
    fi
done
fi

echo "pushed to $done_n instance(s)"
exit $fail
