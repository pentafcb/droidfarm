#!/bin/sh
# Jerusalem 1.0 adaptive screencap loop (canonical copy; baked into the
# image at /opt/screencap-loop.sh, installed into the shared data dir at
# boot by entrypoint.sh, pushed to live containers by push-screencap.sh).
#
# Captures FAST (0.12s) while a viewer is attached, SLOW (2s) when idle.
# On the 8-vCPU fleet host, 20 instances PNG-encoding the framebuffer at
# 8fps 24/7 burns significant CPU for nobody: the dashboard cards show no
# thumbnails, so the only consumers of live.png are active stream viewers
# (MJPEG ws loops, the H.264 broadcaster, WebRTC — all viewer-driven).
#
# Signalling: the manager (local instances) or the wing agent (remote)
# touches /data/media/0/jerusalem/stream_wanted while any viewer is
# connected and re-touches it periodically. A marker older than STALE_AFTER
# seconds counts as stale — so a manager/agent restart that leaves the file
# behind can never pin the loop fast forever — and the loop drops back to
# the idle rate. The 2s idle cadence keeps the host watchdog and the wing
# selfwatch stale-stream checks (>30s) green.
D=${SCREENCAP_DIR:-/data/media/0/jerusalem}
WANT="$D/stream_wanted"
FAST_SLEEP=0.12
IDLE_SLEEP=2
STALE_AFTER=30

want_fast() {
  # Print 1 when a fresh stream_wanted marker exists, else 0. Never fails;
  # any unreadable clock or timestamp degrades to slow (safe) mode.
  [ -f "$WANT" ] || { echo 0; return 0; }
  MT=$(stat -c %Y "$WANT" 2>/dev/null)
  NOW=$(date +%s 2>/dev/null)
  case "$MT" in ''|*[!0-9]*) echo 0; return 0 ;; esac
  case "$NOW" in ''|*[!0-9]*) echo 0; return 0 ;; esac
  if [ $((NOW - MT)) -lt $STALE_AFTER ]; then echo 1; else echo 0; fi
  return 0
}

while true; do
  timeout 5 screencap -p "$D/live.tmp.png" 2>/dev/null
  mv "$D/live.tmp.png" "$D/live.png" 2>/dev/null
  if [ "$(want_fast)" = "1" ]; then
    sleep $FAST_SLEEP
  else
    sleep $IDLE_SLEEP
  fi
done
