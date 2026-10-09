#!/bin/bash
# Jerusalem 1.0 malfunction handler v10 — runs every minute via cron.
# Recovers without deleting data: dead containers, stuck boots, dead helper
# processes (screencap/input/keepalive), stale streams, disk pressure,
# manager self-check, Roblox client version-pin guard, memory pressure,
# Docker container-log trimming.
#
# v4 adds a CIRCUIT BREAKER: every recovery action is counted in a sliding
# window, and an instance that keeps failing is marked DEGRADED — auto-recovery
# pauses (no more infinite restart loops burning CPU) until all checks pass
# green again, which auto-clears the degraded flag. Thresholds:
#   container restarts:      >6 in 60 min  -> DEGRADED
#   helper restarts (each):   >8 in 10 min  -> DEGRADED
#   stale screencap loops:   >10 in 60 min -> DEGRADED
#
# Test overrides (harmless in prod cron): DF_LOG, DF_ST, DF_INSTANCES, DF_VOLBASE,
# DF_MANAGER_URL, DF_CURL_BIN, DF_PINNED_ROBLOX, DF_PROC_MEMINFO (path to a
# fake meminfo), DF_DMESG (fake dmesg executable or file),
# DF_LOG_TRIM_MAX / DF_LOG_TRIM_KEEP (section 10 byte thresholds),
# DF_LOG_GLOB (section 10 log glob, default /var/lib/docker/containers/*/*-json.log).
# v4.1: the .degraded marker now carries the human-readable reason on its first
# line (the manager dashboard reads it to show "needs attention").
# v5: MANAGER SELF-CHECK. The minute-cron now also probes the jerusalem manager
# itself (http://127.0.0.1:8080/, 2xx/3xx = healthy). A wedged-but-alive manager
# (deadlock, thread exhaustion) is invisible to systemd — it never restarts it —
# so 2 consecutive failed probes trigger systemctl start (service down) or
# restart (service up but not answering), counted in the same circuit-breaker
# budget: >2 manager restarts/hour -> global manager.degraded marker (reason on
# first line), auto-recovery pauses until a probe passes again, which
# auto-clears it. Never touches instance data, credentials, or tokens.
# v6: DISK AUTO-RECOVERY + ROBLOX VERSION-PIN GUARD.
#   Disk: at >=90% on /var/lib/docker the watchdog used to only log. Now it
#   runs a bounded safe prune — `docker image prune -f` (dangling images only)
#   + `docker builder prune -f` (build cache only). It NEVER runs volume prune
#   or `image prune -a`: instance data lives in named volumes and the instance
#   image, which those commands could destroy. Budgeted at >2 prunes/hour ->
#   global disk.degraded marker, auto-prune pauses (alert-only) until pressure
#   drops below 90%, which auto-clears the marker. Freed space is logged.
#   Roblox pin: the client must stay on PINNED_ROBLOX (login tap coordinates +
#   device-integrity exposure are tuned to it). Each minute, every booted
#   instance's installed versionName is read via dumpsys and compared. Drift ->
#   one loud ALERT log line per drifted version (deduped by state file), and a
#   matching "back on pinned" line when the version returns. There is NO
#   auto-remediation (a force-updated client can't be rolled back safely) —
#   drift is a human decision, so it is surfaced, never acted on.
# v7: MEMORY PRESSURE WATCH (alert-only). The fleet scaled to 20 instances x
#   2GB limits on a ~31GB host, so the OOM killer is the failure mode the
#   watchdog previously couldn't see. MemAvailable <1024MB is WARN-logged
#   (deduped 3x/hour -> mem.degraded marker with reason, auto-clears on
#   relief); fresh "Out of memory" lines in dmesg are surfaced as WARN. The
#   watchdog NEVER auto-kills or auto-restarts instances for memory — picking
#   the victim is a human decision, surfaced, never acted on.
# v8: ROBLOX KEEPALIVE CRASH-LOOP SURFACING. The in-container keepalive v2
#   budgets launch attempts (12/hour sliding window, state in the shared
#   data volume); over budget it writes keepalive.degraded and backs off to
#   one attempt every 5 min instead of hammering. The watchdog mirrors that
#   marker into the instance .degraded marker (dashboard "needs attention"
#   badge) and counts the instance unhealthy until the keepalive clears its
#   own marker on recovery.
# v9: WAYDROID SESSION DEATH DETECTION + AUTO-RESTART. A dead session is
#   invisible to every other check: the container stays up (entrypoint ends
#   in tail -f), the booted marker persists, and all three helpers run
#   THROUGH `waydroid shell`, so they die too and their restarts fail
#   silently — before v9 the only symptom was a stale stream burning the
#   helper and stale-loop circuit budgets until a misleading DEGRADED.
#   check_one 2b probes the session directly (same getprop idiom as the
#   entrypoint boot check); on death it starts the session detached and
#   skips the helper/stream checks that cycle so their budgets are not
#   burned by a known-dead session. Budgeted >4 starts/hour -> DEGRADED
#   (reason on first line, dashboard badge via instance-health). A 180s
#   grace after each start attempt covers the Android re-boot window.
# v10: DOCKER CONTAINER-LOG TRIMMING (section 10). Docker's json-file driver
#   never rotates container logs by default; each instance container's
#   *-json.log captures all Waydroid session stdout (LXC/Android boot +
#   runtime chatter inherited from `waydroid container/session start`) and
#   can grow to tens of GB — 69G was observed 2026-10-08, starving the disk
#   and plausibly preventing new Android boots. The v6 disk auto-recovery
#   can't touch container logs (only dangling images + build cache), so this
#   section trims any *-json.log over 1GB down to its last 1GB, IN PLACE
#   (copytruncate: same inode, dockerd's open fd stays valid, logging
#   continues; writes past the new EOF are sparse holes, so disk usage really
#   drops). Thresholds match Penta's 2026-10-09 instruction ("trim every
#   hour, keep last 1GB"); the minute-cron enforces it continuously instead
#   of hourly. The last 1GB is always kept for failed-boot post-mortems.
#   Instance data volumes are never touched. Self-limiting: a trimmed log
#   stays under the threshold until it regrows, so no storm budget is needed.
PINNED_ROBLOX="${DF_PINNED_ROBLOX:-2.740.931}"
LOG="${DF_LOG:-/var/log/df-watchdog.log}"
ST="${DF_ST:-/tmp/df-watchdog}"
VOLBASE="${DF_VOLBASE:-/var/lib/docker/volumes}"
mkdir -p "$ST" 2>/dev/null
log() {
  echo "[$(date '+%F %T')] $1" >>"$LOG" 2>/dev/null
  local sz; sz=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
  if [ "$sz" -gt 2097152 ]; then tail -300 "$LOG" >"$LOG.t" 2>/dev/null && mv "$LOG.t" "$LOG" 2>/dev/null; fi
}
bump() { local f="$ST/$1"; local n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$f"; echo "$n"; }
reset() { rm -f "$ST/$1" 2>/dev/null; }

# note_event <key> <window_secs>: record one event in a sliding window,
# prune expired/garbage entries, print the surviving count (incl. this one).
note_event() {
  local key=$1 w=$2 now t tmp n=0
  local f="$ST/$key.ev"   # separate line: $key must be the local, not the outer scope
  now=$(date +%s); tmp="$f.tmp"
  : > "$tmp" 2>/dev/null
  if [ -f "$f" ]; then
    while IFS= read -r t || [ -n "$t" ]; do
      case "$t" in ''|*[!0-9]*) continue;; esac
      if [ $((now - t)) -lt "$w" ]; then printf '%s\n' "$t" >>"$tmp"; n=$((n+1)); fi
    done < "$f"
  fi
  printf '%s\n' "$now" >>"$tmp"; n=$((n+1))
  mv "$tmp" "$f" 2>/dev/null
  printf '%s' "$n"
}

# cb_guard <inst> <eventkey> <window_s> <max> <what>: circuit breaker for one
# recovery action. Prints the event count and returns 0 when the action may
# proceed; marks the instance DEGRADED (logged once) and returns 1 when the
# action budget is exhausted or the instance is already degraded.
cb_guard() {
  local i=$1 key=$2 w=$3 max=$4 what=$5 n
  [ -f "$ST/$i.degraded" ] && return 1
  n=$(note_event "$key" "$w")
  if [ "$n" -gt "$max" ]; then
    # marker carries the reason (first line) for the dashboard "needs attention" badge
    printf '%s\n' "$what restarted $n times in last ${w}s - auto-recovery paused" > "$ST/$i.degraded"
    log "$i DEGRADED: $what restarted $n times in last ${w}s - auto-recovery paused (needs human)"
    return 1
  fi
  printf '%s' "$n"
  return 0
}

check_one() {
  local i=$1
  local c="wd-$i"   # separate line: $i must be the local, not the caller's loop variable
  local base="$VOLBASE/$c-data/_data/data/media/0/jerusalem"
  local healthy=1 n
  # 1. container running? (2 strikes -> start; circuit-broken at 6 restarts/hour)
  if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
    if [ "$(bump "$i.norun")" -ge 2 ]; then
      if n=$(cb_guard "$i" "$i.restart" 3600 6 "container"); then
        log "$i container down -> docker start (restart ${n}/6 this hour)"
        if docker start "$c" >/dev/null 2>&1; then log "$i started ok"; else log "$i start FAILED"; fi
        reset "$i.norun"
      fi
    fi
    return
  fi
  reset "$i.norun"
  # 2. stuck boot? (up >15min, no booted marker -> restart, 2 strikes)
  if [ ! -f "$base/booted" ]; then
    healthy=0
    local start now
    start=$(date -d "$(docker inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null)" +%s 2>/dev/null || echo 0)
    now=$(date +%s)
    if [ "$start" -gt 0 ] && [ $((now - start)) -gt 900 ]; then
      if [ "$(bump "$i.stuck")" -ge 2 ]; then
        if n=$(cb_guard "$i" "$i.restart" 3600 6 "container"); then
          log "$i stuck booting (no marker after 15m) -> docker restart (${n}/6 this hour)"
          docker restart "$c" >/dev/null 2>&1
          reset "$i.stuck"; return
        fi
      fi
    fi
  else
    reset "$i.stuck"
  fi
  # 2b. waydroid session alive? (v9) A dead session is invisible to every
  #     other check: the container stays up, the booted marker persists, and
  #     all three helpers run through `waydroid shell`, so they die too and
  #     their restarts fail silently. Probe the session directly (same
  #     getprop idiom as the entrypoint boot check). On death: start the
  #     session detached, then return early so the helper/stream checks
  #     below don't burn their circuit budgets on a known-dead session.
  #     Only applies once the instance has booted at least once — first
  #     boot stays owned by the stuck-boot check above.
  # --- V9-2B-START ---
  if [ -f "$base/booted" ]; then
    local _sess_ok=0
    if docker exec "$c" waydroid shell getprop sys.boot_completed 2>/dev/null | grep -q '^1$'; then
      _sess_ok=1
    fi
    if [ "$_sess_ok" = "1" ]; then
      rm -f "$ST/$i.sessionstart.ts" 2>/dev/null
    else
      healthy=0
      local _ssnow _ssage=99999
      _ssnow=$(date +%s)
      if [ -f "$ST/$i.sessionstart.ts" ]; then
        _ssage=$(( _ssnow - $(cat "$ST/$i.sessionstart.ts" 2>/dev/null || echo 0) ))
      fi
      if [ "$_ssage" -lt 180 ]; then
        log "$i waydroid session still down (restart issued ${_ssage}s ago, 180s grace) - waiting"
      elif n=$(cb_guard "$i" "$i.session" 3600 4 "waydroid session"); then
        log "$i waydroid session dead -> session start (${n}/4 this hour)"
        if docker exec -d "$c" waydroid session start >/dev/null 2>&1; then
          date +%s > "$ST/$i.sessionstart.ts" 2>/dev/null
          log "$i waydroid session start issued"
        else
          log "$i waydroid session start FAILED"
        fi
      fi
      return
    fi
  fi
  # --- V9-2B-END ---
  # 3. helper processes alive? (screencap loop, input daemon, keepalive)
  local s hk
  for s in screencap-loop.sh input-daemon.sh roblox-keepalive.sh; do
    if ! docker exec "$c" pgrep -f "$s" >/dev/null 2>&1; then
      healthy=0
      hk=$(printf '%s' "$s" | tr -c 'a-zA-Z0-9' '_')
      if n=$(cb_guard "$i" "$i.helper-$hk" 600 8 "$s"); then
        log "$i $s dead -> restart (${n}/8 per 10m)"
        docker exec "$c" nohup waydroid shell sh "/data/media/0/jerusalem/$s" >/dev/null 2>&1 &
      fi
    fi
  done
  # 3b. Roblox keepalive crash-loop surfacing: the in-container keepalive v2
  #     writes keepalive.degraded (reason on first line) when launch attempts
  #     exceed budget and backs off to 5-min attempts. Mirror it as the
  #     instance degraded marker so the dashboard "needs attention" badge
  #     shows it. Not routed through cb_guard — this is surfacing, not
  #     auto-recovery — but while present the instance counts as unhealthy,
  #     so the auto-clear in section 6 waits for the keepalive to recover
  #     (it clears its own marker when Roblox is focused again).
  if [ -f "$base/keepalive.degraded" ]; then
    healthy=0
    if [ ! -f "$ST/$i.degraded" ]; then
      local _kr
      _kr=$(head -c 160 "$base/keepalive.degraded" 2>/dev/null | head -1)
      printf 'Roblox keepalive: %s\n' "${_kr:-launch attempts over budget}" > "$ST/$i.degraded" 2>/dev/null
      log "$i DEGRADED: Roblox keepalive backed off (${_kr:-launch attempts over budget}) - needs human"
    fi
  fi
  # 4. stream fresh? (stale >30s -> restart loop)
  local png="$base/live.png"
  if [ -f "$png" ]; then
    local age=$(( $(date +%s) - $(stat -c %Y "$png" 2>/dev/null || echo 0) ))
    if [ "$age" -gt 30 ]; then
      healthy=0
      if n=$(cb_guard "$i" "$i.staleloop" 3600 10 "screencap-loop"); then
        log "$i screencap stale (${age}s) -> restart loop (${n}/10 per hour)"
        docker exec "$c" pkill -f screencap-loop.sh 2>/dev/null
        docker exec "$c" nohup waydroid shell sh /data/media/0/jerusalem/screencap-loop.sh >/dev/null 2>&1 &
      fi
    fi
  fi
  # 5. Roblox client version-pin guard: login tap coordinates and the
  #    device-integrity exposure are tuned to PINNED_ROBLOX. Drift (auto or
  #    forced update) gets ONE loud ALERT per drifted version; there is no
  #    auto-remediation — a force-updated client needs a human decision.
  if [ -f "$base/booted" ]; then
    _ver=$(docker exec "$c" waydroid shell dumpsys package com.roblox.client 2>/dev/null \
      | grep -m1 '^ *versionName=' | cut -d= -f2 | tr -d '[:space:]')
    if [ -n "$_ver" ]; then
      _vf="$ST/$i.robloxver"
      if [ "$_ver" != "$PINNED_ROBLOX" ]; then
        if [ ! -f "$_vf" ] || [ "$(cat "$_vf" 2>/dev/null)" != "$_ver" ]; then
          printf '%s\n' "$_ver" > "$_vf" 2>/dev/null
          log "ALERT $i Roblox client version DRIFT: installed $_ver (pinned $PINNED_ROBLOX) - login coords + integrity assumptions tuned to pinned build; no auto-remediation, needs human"
        fi
      elif [ -f "$_vf" ]; then
        rm -f "$_vf" 2>/dev/null
        log "$i Roblox client back on pinned build $PINNED_ROBLOX"
      fi
    fi
    # package missing or dumpsys unreadable -> silent skip (install is the
    # entrypoint's job; a missing client is not version drift)
  fi
  # 6. degraded auto-clear: all checks green -> resume auto-recovery
  if [ "$healthy" -eq 1 ] && [ -f "$ST/$i.degraded" ]; then
    rm -f "$ST/$i.degraded" "$ST/$i.restart.ev" "$ST/$i.staleloop.ev" "$ST/$i.session.ev" "$ST/$i.sessionstart.ts" "$ST"/$i.helper-*.ev 2>/dev/null
    log "$i recovered from DEGRADED (all checks green) - auto-recovery resumed"
  fi
}

# Discover managed local instances from the manager state (no hard-coded list).
# Falls back to rbx1-5 if the state file is unreadable.
INSTANCES="${DF_INSTANCES:-}"
if [ -z "$INSTANCES" ]; then
  if [ -f /root/jerusalem/instances.json ]; then
    INSTANCES=$(python3 -c "
import json
try:
    d = json.load(open('/root/jerusalem/instances.json'))
    print(' '.join(n for n, v in d.items() if isinstance(v, dict) and v.get('provider', 'local') == 'local'))
except Exception:
    pass" 2>/dev/null)
  fi
  [ -z "$INSTANCES" ] && INSTANCES="rbx1 rbx2 rbx3 rbx4 rbx5"
fi

# per-instance checks in parallel so the minute-cron stays fast
for i in $INSTANCES; do check_one "$i" & done
wait

# 7. manager service self-check (wedged-but-alive is invisible to systemd):
#    probe the dashboard root; 2 consecutive failures -> systemctl start
#    (service down) or restart (service up but not answering). Circuit-broken:
#    >2 manager restarts/hour -> global manager.degraded marker, auto-recovery
#    pauses until a probe passes again (auto-clears).
MGR_URL="${DF_MANAGER_URL:-http://127.0.0.1:8080/}"
CURL_BIN="${DF_CURL_BIN:-curl}"
if ! command -v "$CURL_BIN" >/dev/null 2>&1; then
  # never fail silently: one WARN line per day when the probe tool is missing
  _nc="$ST/mgr.nocurl.$(date +%F)"
  if [ ! -f "$_nc" ]; then
    rm -f "$ST"/mgr.nocurl.2* 2>/dev/null
    touch "$_nc" 2>/dev/null
    log "WARN curl ($CURL_BIN) missing - manager self-check skipped today"
  fi
else
  _code=$("$CURL_BIN" -s -o /dev/null -w '%{http_code}' --max-time 8 "$MGR_URL" 2>/dev/null || echo "000")
  case "$_code" in
    2*|3*)
      reset "mgr.httpdown"
      if [ -f "$ST/manager.degraded" ]; then
        rm -f "$ST/manager.degraded" "$ST"/mgr.*.ev 2>/dev/null
        log "manager recovered from DEGRADED (HTTP $_code healthy) - auto-recovery resumed"
      fi
      ;;
    *)
      _strikes=$(bump "mgr.httpdown")
      if [ "$_strikes" -ge 2 ]; then
        if _n=$(cb_guard "manager" "mgr.restart" 3600 2 "manager service"); then
          if systemctl is-active --quiet jerusalem 2>/dev/null; then
            log "manager HTTP $_code twice, service active -> systemctl restart jerusalem (${_n}/2 this hour)"
            if systemctl restart jerusalem 2>/dev/null; then log "manager restarted ok"; else log "manager restart FAILED"; fi
          else
            log "manager HTTP $_code twice, service not active -> systemctl start jerusalem (${_n}/2 this hour)"
            if systemctl start jerusalem 2>/dev/null; then log "manager started ok"; else log "manager start FAILED"; fi
          fi
          reset "mgr.httpdown"
        fi
      else
        log "manager HTTP probe failed (code $_code, strike $_strikes/2)"
      fi
      ;;
  esac
fi

# 8. disk pressure: bounded SAFE auto-recovery at >=90%, then alert.
#    Only ever prunes dangling images + build cache. NEVER volume prune or
#    `image prune -a` — instance data lives in named volumes + the instance
#    image. Budgeted: >2 prunes/hour without relief -> disk.degraded marker,
#    auto-prune pauses (alert-only) until usage drops below 90%.
disk_use() { df /var/lib/docker 2>/dev/null | awk 'NR==2{sub(/%/,"",$5); print $5}'; }
usep=$(disk_use)
if [ -n "$usep" ] && [ "$usep" -lt 90 ] && [ -f "$ST/disk.degraded" ]; then
  rm -f "$ST/disk.degraded" "$ST"/disk.prune.ev 2>/dev/null
  log "disk pressure relieved (${usep}%) - auto-prune resumed"
fi
if [ -n "$usep" ] && [ "$usep" -ge 90 ]; then
  if [ -f "$ST/disk.degraded" ]; then
    log "WARN disk ${usep}% on /var/lib/docker - auto-prune paused (budget exhausted, needs human)"
  else
    _pn=$(note_event "disk.prune" 3600)
    if [ "$_pn" -gt 2 ]; then
      printf '%s\n' "disk auto-prune ran ${_pn} times in last 3600s without relieving pressure - auto-prune paused" > "$ST/disk.degraded"
      log "disk DEGRADED: auto-prune ran ${_pn}x this hour, still ${usep}% - paused (needs human)"
    else
      _before="$usep"
      _freed=$(docker image prune -f 2>/dev/null | grep -i 'total reclaimed' | head -1)
      docker builder prune -f >/dev/null 2>&1
      usep=$(disk_use)
      log "disk ${_before}% -> safe prune (dangling images + build cache, ${_pn}/2 per hour): ${_freed:-done}; now ${usep:-?}%"
    fi
  fi
fi
if [ -n "$usep" ] && [ "$usep" -ge 90 ]; then
  log "WARN disk ${usep}% on /var/lib/docker (alert only)"
fi

# 9. memory pressure (v7): alert-only, never auto-kills.
#    The fleet scaled to 20 instances x 2GB limits on a ~31GB host (40GB of
#    limits), so OOM kills are a real risk. At <1024MB MemAvailable the
#    watchdog raises the alarm (deduped); picking the victim stays human.
#    Fresh OOM-killer invocations in dmesg are surfaced too.
_memkb=$(awk '/^MemAvailable:/{print $2}' "${DF_PROC_MEMINFO:-/proc/meminfo}" 2>/dev/null)
if [ -n "$_memkb" ]; then
  if [ "$_memkb" -ge 1048576 ]; then
    if [ -f "$ST/mem.degraded" ]; then
      rm -f "$ST/mem.degraded" "$ST"/mem.low.ev 2>/dev/null
      log "memory pressure relieved ($((_memkb/1024))MB available) - alerts resumed"
    fi
  else
    _mn=$(note_event "mem.low" 3600)
    if [ "$_mn" -ge 3 ] && [ ! -f "$ST/mem.degraded" ]; then
      printf '%s\n' "host MemAvailable $((_memkb/1024))MB - instance limits overcommit RAM (needs human)" > "$ST/mem.degraded"
      log "memory DEGRADED: MemAvailable $((_memkb/1024))MB seen 3x this hour - paused (needs human)"
    elif [ "$_mn" -lt 3 ]; then
      log "WARN memory pressure: MemAvailable $((_memkb/1024))MB (report only, ${_mn}/3 this hour)"
    fi
  fi
fi
# DF_DMESG: path to a file whose contents stand in for `dmesg` output.
if [ -n "$DF_DMESG" ]; then
  _oomc=$(grep -ci "out of memory" "$DF_DMESG" 2>/dev/null || true)
else
  _oomc=$(dmesg 2>/dev/null | grep -ci "out of memory" || true)
fi
if [ -n "$_oomc" ]; then
  _prev=$(cat "$ST/oom.count" 2>/dev/null || echo 0)
  case "$_prev" in ''|*[!0-9]*) _prev=0;; esac
  if [ "$_oomc" -gt "$_prev" ]; then
    note_event "mem.oom" 86400 >/dev/null
    log "WARN OOM killer fired $((_oomc - _prev))x since last check (report only - an instance may be dead, check docker ps)"
  fi
  echo "$_oomc" > "$ST/oom.count"
fi

# --- SECTION 10: docker container-log trimming ---
# Docker's json-file driver never rotates container logs by default. Each
# instance container's *-json.log captures ALL Waydroid session stdout and
# can grow to tens of GB (69G observed 2026-10-08). Trim in place to the
# last 1GB (copytruncate keeps the same inode, so dockerd's open fd stays
# valid and logging continues). Thresholds follow Penta's 2026-10-09
# instruction ("trim every hour, keep last 1GB"); the minute-cron enforces
# it continuously. Never touches instance data volumes.
TRIM_MAX="${DF_LOG_TRIM_MAX:-1073741824}"
TRIM_KEEP="${DF_LOG_TRIM_KEEP:-1073741824}"
TRIM_GLOB="${DF_LOG_GLOB:-/var/lib/docker/containers/*/*-json.log}"
for _jl in $TRIM_GLOB; do
  [ -f "$_jl" ] || continue
  _js=$(stat -c%s "$_jl" 2>/dev/null || echo 0)
  if [ "$_js" -gt "$TRIM_MAX" ]; then
    _t="$_jl.trim"
    if tail -c "$TRIM_KEEP" "$_jl" >"$_t" 2>/dev/null && cat "$_t" >"$_jl" 2>/dev/null; then
      rm -f "$_t" 2>/dev/null
      _before=$(numfmt --to=iec "$_js" 2>/dev/null || echo "$_js")
      _after=$(numfmt --to=iec "$TRIM_KEEP" 2>/dev/null || echo "$TRIM_KEEP")
      log "log-trim: trimmed $(basename "$(dirname "$_jl")") json.log ${_before} -> ${_after} (in-place, fd-safe)"
      note_event "log.trim" 86400 >/dev/null
    else
      log "WARN log-trim: failed to trim $_jl"
      rm -f "$_t" 2>/dev/null
    fi
  fi
done
unset _jl _js _t TRIM_MAX TRIM_KEEP TRIM_GLOB
# --- END SECTION 10 ---

exit 0
