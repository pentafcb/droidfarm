#!/bin/sh
# android-optimize.sh — Android OS optimizer for Waydroid instances (Jerusalem 1.0).
#
# CANONICAL SOURCE: waydroid-image/android-optimize.sh in the repo.
# Installed into every container at boot by entrypoint.sh (from
# /opt/android-optimize.sh, copied there by the Dockerfile), and pushed to
# already-running containers by pending-deploy/android-optimizer/push-optimizer.sh.
#
# Uses ONLY commands verified to work in this Waydroid build: pm, svc, dumpsys.
# Deliberately absent:
#   - `settings put` — broken in this build (AppOps NPE from the root shell,
#     all tables; verified 2026-10-06). Animation-scale tuning is NOT possible
#     here, so this script does not attempt it.
#   - `cmd package bg-dexopt-job` — blocked (SecurityException; verified
#     2026-10-06).
#
# Safe + idempotent: re-running changes nothing. Never touches instance data,
# credentials, or tokens. Logs to /data/media/0/jerusalem/optimize.log.
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
