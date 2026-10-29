#!/bin/bash
# push-booted-watcher.sh — start the booted-watcher loop in every running
# local instance WITHOUT an image rebuild.
#
# Run ON THE MAIN VPS as root:
#   bash /root/jerusalem-deploy/push-booted-watcher.sh [/root/jerusalem/booted-watcher.sh]
#
# SAFE: only copies one script into each container's /opt and starts one
# background loop per container. Never touches instance data, credentials,
# tokens, or any other helper. Idempotent: re-running replaces the script
# and restarts the loop.
#
# Every docker call goes through dex() (bounded by DEX_TIMEOUT, default 30s,
# env-overridable; garbage -> 30; bare docker when the timeout binary is
# missing) so one wedged container or daemon fails that instance instead of
# hanging the whole push. Per-instance [i/N] progress lines show the
# operator exactly where a hang was.
set -u

SRC="${1:-/root/jerusalem/booted-watcher.sh}"

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
    if ! dex cp "$SRC" "$c:/opt/booted-watcher.sh" >/dev/null 2>&1; then
        echo "[$i/$total] $c: docker cp FAILED"
        fail=1
        continue
    fi
    dex exec "$c" chmod +x /opt/booted-watcher.sh >/dev/null 2>&1
    dex exec "$c" pkill -f booted-watcher.sh >/dev/null 2>&1  # idempotent re-push
    # Same idiom as the host watchdog's helper restarts: nohup + detached.
    dex exec "$c" nohup /opt/booted-watcher.sh >/dev/null 2>&1 &
    sleep 2
    if dex exec "$c" pgrep -f booted-watcher.sh >/dev/null 2>&1; then
        echo "[$i/$total] $c: booted-watcher running"
        done_n=$((done_n + 1))
    else
        echo "[$i/$total] $c: booted-watcher start FAILED (entrypoint starts it on next container start)"
        fail=1
    fi
done
fi

echo "started in $done_n instance(s)"
exit $fail
