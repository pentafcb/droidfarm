#!/bin/sh
# boot-complete.sh — full Android boot-completion sequence for a Waydroid instance.
#
# PROBLEM IT FIXES: entrypoint.sh waits at most 5 minutes for sys.boot_completed.
# On a slow first boot (fleet creation under load), that window closes with the
# whole "booted" block skipped: no Roblox install, no canonical helper scripts
# in the data dir, no screencap loop / input daemon / keepalive, no optimizer,
# no Roblox launch. booted-watcher.sh later writes the `booted` marker, so the
# dashboard stops saying "booting" — but the instance is a shell: the host
# watchdog's helper-restart then runs `waydroid shell sh .../<helper>.sh` on
# files that were never installed (silent failure), burns the helper circuit
# budget, and trips a misleading DEGRADED.
#
# This script IS the boot-completion sequence, extracted verbatim from
# entrypoint.sh so both callers run the exact same steps:
#   - fast path: entrypoint.sh calls it when its 5-min wait sees BOOTED=1
#   - late path: booted-watcher.sh calls it when it observes a late
#     sys.boot_completed=1 with no `booted` marker yet
#
# Idempotent: safe on every container boot and safe to re-run after a partial
# run (helpers are pgrep-guarded against duplicates, the Roblox install is
# skipped when the package is present, script installs are plain overwrites).
# Never deletes instance data, markers, credentials, or tokens. Never fails
# hard — every step is best-effort with a WARN; a setup hiccup must never
# wedge the boot. Exits 0 always (unless the shell itself is broken).
#
# Test seams (env only, no-ops in prod):
#   JERUSALEM_DATA_DIR  container-side data dir
#                       (default /root/.local/share/waydroid/data/media/0/jerusalem)
#   BOOT_COMPLETE_OPT   dir holding the canonical /opt scripts (default /opt)
S="${JERUSALEM_DATA_DIR:-/root/.local/share/waydroid/data/media/0/jerusalem}"
OPT="${BOOT_COMPLETE_OPT:-/opt}"
PKG="com.roblox.client"
# Android-side path of $S (helpers run via `waydroid shell`).
A_S="/data/media/0/jerusalem"

# Bounded Android calls. A wedged Waydroid session hangs `waydroid shell`
# forever. The three foreground calls in the install block below used to be
# unbounded: a hang wedged this whole script AFTER the `booted` marker was
# written above, so the dashboard showed "ready", the stuck-boot check never
# fired (marker present), and the instance sat with no helpers and no Roblox
# - a silent wedge. bcs() bounds them (GNU timeout, present in this image;
# falls back to the plain call when the binary is missing). A timeout
# (rc 124) is UNKNOWN, not failure: WARN and continue so the helpers still
# start. Timeouts are env-overridable for tests; garbage values degrade to
# a fast failure, never a hang.
BC_PM_LIST_TIMEOUT_S="${BC_PM_LIST_TIMEOUT_S:-30}"
BC_PM_INSTALL_TIMEOUT_S="${BC_PM_INSTALL_TIMEOUT_S:-300}"
BC_KEYEVENT_TIMEOUT_S="${BC_KEYEVENT_TIMEOUT_S:-15}"
bcs() { # bcs <secs> <waydroid args...>: timeout-bounded `waydroid`
    _bcs_secs="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$_bcs_secs" waydroid "$@"
    else
        waydroid "$@"
    fi
}

log() { echo "[boot-complete] $*"; }

mkdir -p "$S" 2>/dev/null || log "WARN: cannot mkdir $S (continuing)"
touch "$S/booted" 2>/dev/null || log "WARN: cannot write $S/booted"

# --- Roblox install (split APKs, pinned 2.740.931) ---
_bcs_list="$(bcs "$BC_PM_LIST_TIMEOUT_S" shell pm list packages 2>/dev/null)"; _bcs_rc=$?
[ "$_bcs_rc" -eq 124 ] && log "WARN: pm list packages timed out after ${BC_PM_LIST_TIMEOUT_S}s - attempting install anyway."
if echo "$_bcs_list" | grep -q "$PKG"; then
    log "Roblox already installed."
    touch "$S/roblox_done" 2>/dev/null || true
    # A previous failed-install marker (e.g. from an earlier boot where the
    # install timed out) is stale now: the client is present, so clear it.
    # The host watchdog mirrors this marker to the dashboard "needs
    # attention" badge and auto-clears its own degraded marker once this
    # file is gone (see df-watchdog.sh check 3c).
    rm -f "$S/roblox_install_failed" 2>/dev/null || true
else
    log "installing Roblox 2.740.931 (split APKs)..."
    if [ -f "$OPT/roblox-base.apk" ] && [ -f "$OPT/roblox-x86_64.apk" ]; then
        mkdir -p "$S/../apks" 2>/dev/null
        cp -f "$OPT/roblox-base.apk" "$S/../apks/" 2>/dev/null
        cp -f "$OPT/roblox-x86_64.apk" "$S/../apks/" 2>/dev/null
        _bcs_out="$(bcs "$BC_PM_INSTALL_TIMEOUT_S" shell pm install /data/media/0/apks/roblox-base.apk /data/media/0/apks/roblox-x86_64.apk 2>&1)"; _bcs_rc=$?
        if [ "$_bcs_rc" -eq 124 ]; then
            log "WARN: Roblox install timed out after ${BC_PM_INSTALL_TIMEOUT_S}s (wedged session?) - skipping install, helpers will still start."
            printf '%s\n' "pm install timed out after ${BC_PM_INSTALL_TIMEOUT_S}s (wedged session?)" > "$S/roblox_install_failed" 2>/dev/null || true
        elif echo "$_bcs_out" | grep -q "Success"; then
            log "Roblox installed successfully."
            touch "$S/roblox_done" 2>/dev/null || true
            rm -f "$S/roblox_install_failed" 2>/dev/null || true
        else
            # The install is one-shot at first boot: without a marker the
            # dashboard card would sit on "installing_roblox" forever with
            # no alert (the keepalive skips uninstalled packages). Record
            # the failure reason (first line, 160-char cap) for the host
            # watchdog to surface (df-watchdog.sh check 3c).
            _bcs_reason="$(printf '%s' "$_bcs_out" | head -1 | head -c 160)"
            log "WARN: Roblox install failed."
            printf '%s\n' "pm install failed: ${_bcs_reason:-no output}" > "$S/roblox_install_failed" 2>/dev/null || true
        fi
    else
        log "WARN: Roblox APKs not found under $OPT, skipping install."
        printf '%s\n' "APKs missing from image ($OPT/roblox-*.apk)" > "$S/roblox_install_failed" 2>/dev/null || true
    fi
fi
bcs "$BC_KEYEVENT_TIMEOUT_S" shell input keyevent 4 2>/dev/null; _bcs_rc=$?
[ "$_bcs_rc" -eq 124 ] && log "WARN: dismiss keyevent timed out after ${BC_KEYEVENT_TIMEOUT_S}s (continuing)"

# --- persistent screencap loop for fast dashboard streaming ---
# Adaptive rate (see waydroid-image/screencap-loop.sh): 0.12s while a viewer
# is attached, 2s when idle. Writes to /data/media/0/jerusalem/live.png which
# the host reads directly from the docker volume (no per-frame exec overhead).
# Atomic rename avoids the dashboard reading a partial file (black flicker).
mkdir -p "$S" 2>/dev/null
if [ -f "$OPT/screencap-loop.sh" ]; then
    cp -f "$OPT/screencap-loop.sh" "$S/screencap-loop.sh"
    log "adaptive screencap loop installed from $OPT/screencap-loop.sh."
else
    log "WARN $OPT/screencap-loop.sh missing - legacy inline fast-only loop"
    printf '#!/bin/sh\nwhile true; do\n  timeout 5 screencap -p /data/media/0/jerusalem/live.tmp.png 2>/dev/null\n  mv /data/media/0/jerusalem/live.tmp.png /data/media/0/jerusalem/live.png 2>/dev/null\n  sleep 0.12\ndone\n' > "$S/screencap-loop.sh"
fi
chmod +x "$S/screencap-loop.sh" 2>/dev/null || true

# --- input daemon: persistent shell reading commands from a FIFO ---
# (avoids the 27s waydroid-shell startup per tap; the API writes to the FIFO)
printf '#!/bin/sh\nFIFO=/data/media/0/jerusalem/input.fifo\nrm -f $FIFO\nmkfifo $FIFO\nwhile true; do\n  if read cmd < $FIFO; then\n    eval "$cmd" 2>/dev/null\n  fi\ndone\n' > "$S/input-daemon.sh" 2>/dev/null
chmod +x "$S/input-daemon.sh" 2>/dev/null || true

# --- Roblox keepalive: force Roblox to always stay in foreground ---
# Uses input taps (monkey/am do not work in this Waydroid image).
# v2 (crash-loop circuit breaker: launch-attempt budget, backoff, and a
# keepalive.degraded marker the host watchdog surfaces on the dashboard).
if [ -f "$OPT/roblox-keepalive.sh" ]; then
    cp -f "$OPT/roblox-keepalive.sh" "$S/roblox-keepalive.sh"
    log "Roblox keepalive v2 installed from $OPT/roblox-keepalive.sh."
else
    log "WARN $OPT/roblox-keepalive.sh missing - legacy inline keepalive (no crash-loop breaker)"
    printf '#!/bin/sh\nwhile true; do\n  FOCUS=$(dumpsys window 2>/dev/null | grep "mCurrentFocus" | head -1)\n  case "$FOCUS" in\n    *com.roblox.client*) ;;\n    *) input keyevent 3; sleep 1; input swipe 512 400 512 100; sleep 2; input tap 129 424 ;;\n  esac\n  sleep 10\ndone\n' > "$S/roblox-keepalive.sh"
fi
chmod +x "$S/roblox-keepalive.sh" 2>/dev/null || true

# --- start helpers (pgrep-guarded: no duplicates when re-run) ---
# (the host watchdog also restarts any of these it finds dead)
start_helper() { # $1 = script name in $S
    if [ ! -f "$S/$1" ]; then
        log "WARN: $S/$1 missing - cannot start $1"
        return 0
    fi
    if pgrep -f "$1" >/dev/null 2>&1; then
        log "$1 already running."
    else
        nohup waydroid shell sh "$A_S/$1" >/dev/null 2>&1 &
        log "$1 started."
    fi
    return 0
}
start_helper "screencap-loop.sh"
start_helper "input-daemon.sh"
start_helper "roblox-keepalive.sh"

# --- Android OS optimization: debloat, stay-awake, no-doze, cache drop ---
# NOTE: `settings put` is broken in this Waydroid build (AppOps NPE from the
# root shell), so the optimizer uses only pm/svc/dumpsys (see
# waydroid-image/android-optimize.sh).
if [ -f "$OPT/android-optimize.sh" ]; then
    cp -f "$OPT/android-optimize.sh" "$S/android-optimize.sh"
    log "Android optimizer installed from $OPT/android-optimize.sh."
else
    log "WARN $OPT/android-optimize.sh missing - legacy inline optimizer (image predates the canonical file)"
    cat > "$S/android-optimize.sh" <<'OPT_EOF'
#!/bin/sh
S=/data/media/0/jerusalem
{
echo "[optimize] run at $(date)"
# Debloat: disable unused system packages (reversible, user 0 only)
for p in com.android.printspooler com.android.wallpaper.livepicker com.android.stk com.android.cellbroadcastreceiver com.android.bookmarkprovider; do
  pm disable-user --user 0 "$p" >/dev/null 2>&1
done
# Never sleep while streaming (black-screen killer); persists in settings
svc power stayon true >/dev/null 2>&1
# Disable Doze (runtime state; re-applied every boot by this script)
dumpsys deviceidle disable >/dev/null 2>&1
# Trim app caches to free storage
pm trim-caches 999G >/dev/null 2>&1
echo "[optimize] done"
} >> $S/optimize.log 2>&1
OPT_EOF
fi
chmod +x "$S/android-optimize.sh" 2>/dev/null || true
waydroid shell sh "$A_S/android-optimize.sh" >/dev/null 2>&1 &
log "Android optimizer started."

# --- launch Roblox immediately (tap sequence; monkey does not work here) ---
waydroid shell "input keyevent 3; sleep 1; input swipe 512 400 512 100; sleep 2; input tap 129 424" >/dev/null 2>&1 &
log "Roblox launch issued."

log "boot-complete done."
exit 0
