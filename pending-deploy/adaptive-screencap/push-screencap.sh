#!/bin/bash
# push-screencap.sh — push the adaptive screencap loop into live wd-* containers.
# Run ONCE on the VPS as root AFTER apply-all stages
# /root/jerusalem/screencap-loop.sh. Idempotent: safe to re-run.
#
# For each running wd-* container: copy the canonical loop into its shared
# data dir (the same file df-watchdog.sh restarts on the minute cron, so
# even a failed restart here self-heals), restart the loop, verify.
#
# Every docker call goes through dex() (bounded by DEX_TIMEOUT, default 30s,
# env-overridable; garbage -> 30; bare docker when the timeout binary is
# missing) so one wedged container or daemon fails that instance instead of
# hanging the whole push. Per-instance [i/N] progress lines show the
# operator exactly where a hang was.
set -u

# Env overrides (also used by unit tests of the post-deploy orchestrator):
#   PUSH_SCREENCAP_SRC      canonical loop file (default /root/jerusalem/screencap-loop.sh)
#   PUSH_SCREENCAP_VOLBASE  docker volumes dir (default /var/lib/docker/volumes)
#   DEX_TIMEOUT             seconds per docker call (default 30)
SRC="${PUSH_SCREENCAP_SRC:-/root/jerusalem/screencap-loop.sh}"
VOLBASE="${PUSH_SCREENCAP_VOLBASE:-/var/lib/docker/volumes}"
REL="data/media/0/jerusalem/screencap-loop.sh"
LOOP_IN_CONTAINER="/data/media/0/jerusalem/screencap-loop.sh"
FAIL=0
DONE=0

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

[ -f "$SRC" ] || { echo "FAIL: $SRC missing (run apply-all first)"; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "FAIL: docker not found"; exit 1; }

# Fail closed when the daemon itself is wedged: an empty instance list with
# exit 0 would silently "succeed" while pushing nothing.
ps_out=$(dex ps --format '{{.Names}}' 2>/dev/null); ps_rc=$?
if [ "$ps_rc" -ne 0 ]; then
    echo "FAIL: docker ps failed (rc=$ps_rc) - docker daemon may be wedged"
    exit 1
fi
mapfile -t _CS < <(printf '%s\n' "$ps_out" | grep '^wd-' | sort)
total=${#_CS[@]}

i=0
if [ "$total" -gt 0 ]; then
for c in "${_CS[@]}"; do
    i=$((i + 1))
    name=${c#wd-}
    dest="$VOLBASE/${c}-data/_data/$REL"
    if ! cp -f "$SRC" "$dest" 2>/dev/null; then
        echo "[$i/$total] $name: copy FAILED"; FAIL=1; continue
    fi
    chmod +x "$dest"
    # Stop the old loop (twice: catch one that was mid-screencap), then
    # restart exactly the way df-watchdog.sh does.
    dex exec "$c" pkill -f screencap-loop.sh 2>/dev/null
    sleep 1
    dex exec "$c" pkill -f screencap-loop.sh 2>/dev/null
    dex exec "$c" nohup waydroid shell sh "$LOOP_IN_CONTAINER" >/dev/null 2>&1 &
    sleep 2
    if dex exec "$c" pgrep -f "screencap-loop.sh" >/dev/null 2>&1; then
        echo "[$i/$total] $name: adaptive loop running"; DONE=$((DONE + 1))
    else
        echo "[$i/$total] $name: RESTART FAILED (df-watchdog will retry within a minute)"
        FAIL=1
    fi
done
fi

echo "pushed to $DONE container(s)"
exit $FAIL
