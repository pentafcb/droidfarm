#!/bin/bash
# Jerusalem 1.0 malfunction handler v11 — runs every minute via cron.
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
# DF_LOG_GLOB (section 10 log glob, default /var/lib/docker/containers/*/*-json.log),
# DF_LOCKDIR (lock dir, default /tmp/df-watchdog.lock),
# DF_LOCK_STALE_SECS (stale-lock age, default 600),
# DF_BOOT_WINDOW_S (fresh-boot window, default 900),
# DF_BOOT_MAX_STARTS (staggered starts per cycle, default 3),
# DF_BOOT_ATTEMPTS (per-instance boot-start attempts before DEGRADED, default 3),
# DF_PROC_UPTIME (path to uptime file, default /proc/uptime),
# DF_BINDERFS (binderfs mount, default /dev/binderfs),
# DF_BINDER_COUNT (pool size, default 64).
# DF_DW_FAST / DF_DW_ACTION / DF_DW_SLOW (v13 docker-call timeouts in secs).
# v4.1: the .degraded marker now carries the human-readable reason on its first
# line (the manager dashboard reads it to show "needs attention").
# v5: MANAGER SELF-CHECK. The minute-cron now also probes the jerusalem manager
# itself (http://127.0.0.1:8080/, 2xx/3xx = healthy; v12 additionally requires
# a COMPLETE response body — a 2xx with a truncated body counts as failed). A wedged-but-alive manager
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
#   one loud ALERT log line per drifted version (deduped by state file), a
#   <instance>.roblox_drift marker (reason on first line -> dashboard badge),
#   and a matching "back on pinned" line + marker removal when the version
#   returns. There is NO auto-remediation (a force-updated client can't be
#   rolled back safely) — drift is a human decision, so it is surfaced,
#   never acted on.
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
# v11: SINGLE-INSTANCE LOCK (mkdir mutex, same pattern as the Windows
#   installer). The minute-cron spawns this script every 60s; if `docker`
#   hangs (wedged daemon — the exact failure the v5 manager self-check
#   guards against at the service level), every cron run spawns another
#   watchdog, each spawning more docker subprocesses, stacking forever with
#   nothing to reap them. The lock serializes runs: a second run while one
#   holds the lock exits silently; a lock older than DF_LOCK_STALE_SECS
#   (default 600s — a healthy run takes well under a minute) is treated as
#   stale (previous run crashed or was SIGKILLed) and taken over with one
#   WARN log line. The EXIT trap releases only OUR lock (pid-checked), so a
#   hung run that lost a stale-takeover race can never delete the new
#   holder's lock; backgrounded per-instance checks clear the inherited
#   EXIT trap so they can't release the main run's lock early.
# v12: MANAGER PROBE REQUIRES A COMPLETE BODY. A starved/thread-exhausted
#   manager answers 200 with a fast TTFB and then closes the connection
#   mid-body (curl exit 18) — the old `-o /dev/null` probe only checked the
#   status line, so that read as "healthy" forever and the last-resort
#   auto-recovery never fired. Section 7 now fetches the body to a temp
#   file and requires BOTH curl exit 0 (complete transfer) and 2xx/3xx
#   status; a 2xx/3xx with a truncated body is a failed probe with a
#   distinct "body truncated" log line, on the same 2-strike restart logic.
#   Temp files are removed after every probe. False-positive risk is
#   ~nil: the probe hits localhost and a healthy manager always completes
#   the small page.
# v13: BOUNDED DOCKER CALLS. Every docker invocation below goes through
#   dw() (timeout-wrapped). A hung docker — wedged daemon, or a wedged
#   waydroid session making `docker exec` hang — used to freeze the whole
#   minute-cron run forever: the v11 lock only serialized runs, so each new
#   holder hung on the same wedged call and NO check ever ran, including
#   the manager self-check (the last-resort auto-recovery). Now a hung call
#   fails after N seconds (exit 124) and the run always completes: the lock
#   releases, the manager self-check runs every minute, and the next minute
#   retries. A timeout is UNKNOWN, not dead: the session/helper probes skip
#   recovery that cycle (one WARN per instance per day) instead of issuing
#   restarts against a possibly-live box; everywhere else a timeout behaves
#   like any other docker failure (strikes, FAILED logs, circuit budgets —
#   the machinery built for exactly this). The two backgrounded helper
#   restarts stay bare `docker` on purpose: the exec client stays attached
#   to the looping helper for its whole life, and a timeout there would
#   kill the helper it just started.
# v15: DOCKER-DAEMON GUARD. v13 bounded hung calls but a DOWN daemon still
#   looked exactly like 20 dead containers: `docker ps` fails fast (daemon
#   connection refused, not a timeout), so section 1's container-down branch
#   fired for every instance — 2 strikes -> `docker start`, which also
#   fails, and within the hour the 6/hour circuit breaker marked EVERY
#   instance DEGRADED and paused auto-recovery farm-wide. The root cause
#   (dead dockerd) was never repaired, the markers lied about the cause,
#   and even after a human restarted dockerd nothing recovered (section 6
#   auto-clear needs all-green checks, which down containers never give).
#   Section 0b now probes the daemon itself (`docker info`) before ANY
#   docker-dependent recovery: while it fails, boot recovery, the
#   per-instance loop, and the disk auto-prune are all skipped (no strikes
#   burned, no budgets tripped, no misleading markers), and after 2
#   consecutive failed probes the watchdog restarts dockerd itself
#   (same 2-strike + 2/hour cb_guard shape as the v5 manager self-check).
#   A dockerd restart is recoverable by design: `docker run` sets no
#   --restart policy, so stopped containers come back through the normal
#   section-1 start path once the probe passes — with clean budgets,
#   because the outage burned none. The disk/memory/manager/log sections
#   are docker-independent and keep running during an outage.
# v16: INSTANCE-NAME GATE. The watchdog reads instances.json keys directly
#   (manager state, no validation on this side) and check_one builds marker
#   paths ($ST/$i.degraded, $ST/$i.robloxver) and container names (wd-$i)
#   straight from the key — a corrupted record carrying "../../tmp/x"
#   would write marker files outside $ST and hand docker nonsense names.
#   The discovery block now filters to the manager's own contract
#   (^[A-Za-z0-9_-]{1,32}$, see valid_instance_name) before either loop
#   runs, with one WARN line per cycle naming the skipped keys.
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

# --- v11: single-instance lock ---
LOCKD="${DF_LOCKDIR:-/tmp/df-watchdog.lock}"
LOCK_STALE="${DF_LOCK_STALE_SECS:-600}"
_acquire_lock() {
  if mkdir "$LOCKD" 2>/dev/null; then
    echo "$$" >"$LOCKD/pid" 2>/dev/null
    return 0
  fi
  _lm=$(stat -c %Y "$LOCKD" 2>/dev/null || echo "")
  if [ -z "$_lm" ]; then
    # lock dir vanished between the failed mkdir and the stat — retry once
    if mkdir "$LOCKD" 2>/dev/null; then
      echo "$$" >"$LOCKD/pid" 2>/dev/null
      return 0
    fi
    return 1
  fi
  _age=$(( $(date +%s) - _lm ))
  if [ "$_age" -gt "$LOCK_STALE" ]; then
    log "WARN watchdog: stale lock (${_age}s old) — previous run crashed; taking over"
    rm -rf "$LOCKD" 2>/dev/null
    if mkdir "$LOCKD" 2>/dev/null; then
      echo "$$" >"$LOCKD/pid" 2>/dev/null
      return 0
    fi
    return 1
  fi
  return 1  # fresh lock: another run is genuinely in progress — exit silently
}
_release_lock() {
  # Only remove OUR lock: a newer run may have taken over a stale lock while
  # this (hung) process was still alive — never delete someone else's lock.
  [ "$(cat "$LOCKD/pid" 2>/dev/null)" = "$$" ] && rm -rf "$LOCKD" 2>/dev/null
}
if ! _acquire_lock; then
  # No lock and no takeover: either another run holds it (silent exit) or
  # the lock dir itself is broken. Tell them apart so a broken lock dir
  # can't silently disable the watchdog forever.
  [ -d "$LOCKD" ] || log "WARN watchdog: cannot create lock dir $LOCKD — skipping this run"
  exit 0
fi
trap '_release_lock' EXIT
# --- end v11 lock ---

# --- watchdog liveness heartbeat ---
# Touch a heartbeat file on every executed run, AFTER the lock is held: a
# run that exits silently on a foreign lock never touches it. verify-live.sh
# --ssh reads its mtime as proof the minute-cron is actually firing. A
# healthy no-op run logs nothing, so the log mtime can't prove liveness,
# and the lock dir is released at exit, so it can't either. Silent on
# failure — a missing heartbeat surfaces as a WARN in verify-live, not a
# crash here. Test override: DF_ST (part of the standard ST override).
touch "$ST/heartbeat" 2>/dev/null || true

# --- v13: bounded docker calls ---
# dw <secs> <docker args...>: run one docker call with a hard time bound.
# A hung docker (wedged daemon, wedged waydroid session) exits 124 instead
# of freezing the minute-cron run forever. Falls back to bare docker when
# the `timeout` binary is missing (never break the watchdog over it).
DW_FAST="${DF_DW_FAST:-10}"      # cheap probes: ps, inspect, exec pgrep/getprop/dumpsys/pkill
DW_ACTION="${DF_DW_ACTION:-30}"   # start, restart, exec -d session start
DW_SLOW="${DF_DW_SLOW:-120}"     # image/builder prune (can legitimately take a while)
_HAVE_TIMEOUT=0
command -v timeout >/dev/null 2>&1 && _HAVE_TIMEOUT=1
dw() {
  local _t=$1; shift
  if [ "$_HAVE_TIMEOUT" -eq 1 ]; then
    timeout "$_t" docker "$@"
  else
    docker "$@"
  fi
}
_probe_timeout_warn() { # _probe_timeout_warn <inst> <what>: one WARN per instance per day
  local _m="$ST/$1.dw-timeout.$(date +%F)"
  if [ ! -f "$_m" ]; then
    rm -f "$ST"/$1.dw-timeout.2* 2>/dev/null
    touch "$_m" 2>/dev/null
    log "$1 WARN: docker $2 timed out - state unknown, skipping this cycle"
  fi
}
# --- end v13 ---

# v14: HOST-BOOT FLEET RECOVERY (section 0). After a host reboot every wd-*
#   container is stopped (docker run sets no --restart policy) and the
#   binderfs pool has dropped back to the stock set — only the CREATE path
#   (manager ensure_binder_devices) ever restored it. Without this section
#   the minute-cron's section-1 dead-container check fired for EVERY
#   stopped instance in the same pass: a 20-way Waydroid boot storm on the
#   8-vCPU box, and `docker start` on containers whose bind-mounted
#   /dev/binderfs/binderN sources had vanished — docker auto-creates empty
#   directories at missing bind sources, so Waydroid booted with dead
#   binder mounts, wedged at stuck-boot, and burned the 6/hour circuit
#   budget into mass DEGRADED.
#   Section 0 runs before the per-instance loop. Fresh-boot detection: the
#   $ST/boot.done sentinel is missing ($ST is /tmp, wiped by reboot) AND
#   host uptime is under BOOT_WINDOW_S. It then (1) restores the binderfs
#   pool (mount if needed + binder-control writes, the same technique as
#   the manager; empty docker bind-stub directories are rmdir'd — rmdir
#   only removes EMPTY dirs so it can never delete data — and recreated;
#   non-empty stubs are left alone with one ALERT per boot and the fleet
#   start is skipped that cycle), (2) starts stopped instances STAGGERED
#   (BOOT_MAX_STARTS per minute-cron cycle, sorted order, .degraded
#   instances skipped, per-instance BOOT_ATTEMPTS budget before DEGRADED),
#   (3) writes the sentinel when the queue drains. While active, check_one's
#   container-down branch does not start anything (it would race the
#   stagger and re-create the herd). Missed window (uptime >= window, e.g.
#   the cron was down across the boot): sentinel written once, normal
#   per-instance checks own recovery — today's behavior, no regression.
#   Never deletes instance data, credentials, or tokens; never
#   stops/kills/restarts anything except `docker start` on stopped
#   containers.
# v16: BOOT-RECOVERY PAUSE SURFACING (section 0). A paused boot recovery
# (binder pool blocked/incomplete) used to be log-only: the dashboard
# showed plain "stopped" cards while the fleet start was paused. The
# paused paths now write $ST/boot.degraded (reason on the first line),
# surfaced by the manager as a fifth global-health banner entry; it
# clears when the pool completes, the queue drains, or the boot window
# is missed — never outliving the pause.
# v15: ROBLOX INSTALL-FAILURE SURFACING (check 3c). boot-complete.sh's
#   one-shot pm install can fail (bad APKs, package-manager failure) or
#   time out (wedged session) or find no APKs in the image — before v15 it
#   wrote no marker, only one WARN in the container log, so the dashboard
#   card sat on "installing_roblox" forever with zero alert (the keepalive
#   skips uninstalled packages, so nothing ever retried or reported it).
#   boot-complete.sh now writes roblox_install_failed (reason on first
#   line) on every failure path and clears it when the client is present.
#   The watchdog mirrors it into the instance .degraded marker (dashboard
#   "needs attention" badge, same convention as the v8 keepalive mirror)
#   and counts the instance unhealthy until boot-complete clears its own
#   marker on a later successful run. Not routed through cb_guard —
#   surfacing, not auto-recovery: a failed install needs a human (fix the
#   APKs / rebuild the image / recreate the instance).

# --- v14: host-boot fleet recovery ---
BOOT_WINDOW_S="${DF_BOOT_WINDOW_S:-900}"
BOOT_MAX_STARTS="${DF_BOOT_MAX_STARTS:-3}"
BOOT_ATTEMPTS="${DF_BOOT_ATTEMPTS:-3}"
PROC_UPTIME="${DF_PROC_UPTIME:-/proc/uptime}"
BINDERFS="${DF_BINDERFS:-/dev/binderfs}"
BINDER_COUNT="${DF_BINDER_COUNT:-64}"
# sanitize numeric knobs: garbage env degrades to defaults, never a herd
case "$BOOT_WINDOW_S" in ''|*[!0-9]*) BOOT_WINDOW_S=900;; esac
case "$BOOT_MAX_STARTS" in ''|*[!0-9]*|0) BOOT_MAX_STARTS=3;; esac
case "$BOOT_ATTEMPTS" in ''|*[!0-9]*|0) BOOT_ATTEMPTS=3;; esac
case "$BINDER_COUNT" in ''|*[!0-9]*|0) BINDER_COUNT=64;; esac

# boot_recovery_active: true (rc 0) while a host boot still needs recovery.
boot_recovery_active() {
  [ -f "$ST/boot.done" ] && return 1
  local up
  up=$(awk '{print int($1)}' "$PROC_UPTIME" 2>/dev/null)
  case "$up" in ''|*[!0-9]*) return 1;; esac  # uptime unreadable: fail open
  [ "$up" -lt "$BOOT_WINDOW_S" ]
}

# ensure_binder_pool: restore the binderfs device pool after a host reboot.
# Prints the device count on stdout. Sets BOOT_STUB_BLOCKED=1 when a
# non-empty stub directory blocks a device name (human cleanup needed).
# Never fails the caller: every failure path is warn-only.
BOOT_STUB_BLOCKED=0
ensure_binder_pool() {
  local n=0 i=0 name _skip_write
  BOOT_STUB_BLOCKED=0
  if [ ! -e "$BINDERFS/binder-control" ]; then
    command -v modprobe >/dev/null 2>&1 && modprobe binder_linux 2>/dev/null
    mkdir -p "$BINDERFS" 2>/dev/null
    mount -t binder binder "$BINDERFS" 2>/dev/null
  fi
  if [ ! -e "$BINDERFS/binder-control" ]; then
    log "WARN boot recovery: binderfs unavailable at $BINDERFS - pool restore skipped this cycle"
    printf '0 0'
    return 0
  fi
  while [ "$i" -lt "$BINDER_COUNT" ]; do
    name="binder"; [ "$i" -gt 0 ] && name="binder$i"
    _skip_write=0
    if [ -d "$BINDERFS/$name" ]; then
      # docker auto-creates empty directories at missing bind sources on
      # `docker start`. rmdir removes ONLY empty dirs; a populated one is
      # left alone with one ALERT per boot (human cleanup).
      if rmdir "$BINDERFS/$name" 2>/dev/null; then
        log "boot recovery: removed empty docker bind-stub dir $name"
      else
        _skip_write=1
        BOOT_STUB_BLOCKED=1
        if [ ! -f "$ST/boot.stub-alert" ]; then
          touch "$ST/boot.stub-alert" 2>/dev/null
          log "ALERT boot recovery: $BINDERFS/$name is a non-empty directory (not a binder device) - manual cleanup needed, fleet start paused"
        fi
      fi
    fi
    if [ -e "$BINDERFS/$name" ]; then
      n=$((n+1))
    elif [ "$_skip_write" = "0" ] && echo "$name" > "$BINDERFS/binder-control" 2>/dev/null; then
      # write accepted by binder-control (in prod the kernel creates the
      # device on write); a rejected write leaves the count short and the
      # fleet start paused for a retry next cycle.
      n=$((n+1))
    fi
    i=$((i+1))
  done
  printf '%s %s' "$n" "$BOOT_STUB_BLOCKED"
  return 0
}

# boot_recovery: section 0 — restore binder pool, stagger-start the fleet.
# v16: a PAUSED boot recovery is now a global marker, not just a log line.
# Before v16, a blocked binder pool (non-empty stub dir, binderfs
# unavailable, pool short) only produced an ALERT/WARN in the watchdog log:
# the dashboard grid kept showing plain "stopped" cards while the fleet
# start was paused, and the Eventos log scrolled past it. Now the paused
# paths write $ST/boot.degraded (reason on the first line, same convention
# as the other global markers) so the manager's global_watchdog_health()
# surfaces it in the dashboard "Atención del sistema" banner. The marker
# clears as soon as the pool is complete again (the pause is over, even
# while the stagger is still draining), when the queue drains, and when
# the boot window is missed — it never outlives the pause it describes.
boot_recovery() {
  if [ -f "$ST/boot.done" ]; then
    rm -f "$ST/boot.degraded" 2>/dev/null
    return 0
  fi
  local up
  up=$(awk '{print int($1)}' "$PROC_UPTIME" 2>/dev/null)
  case "$up" in ''|*[!0-9]*)
    # uptime unreadable: fail open to today's behavior, retry next cycle.
    return 0;;
  esac
  if [ "$up" -ge "$BOOT_WINDOW_S" ]; then
    log "boot recovery: boot window missed (uptime ${up}s) - normal per-instance checks own recovery"
    rm -f "$ST/boot.degraded" 2>/dev/null
    touch "$ST/boot.done" 2>/dev/null
    return 0
  fi
  log "boot recovery: fresh host boot (uptime ${up}s) - restoring binder pool, starting fleet staggered"
  local have stubblocked
  set -- $(ensure_binder_pool)
  have=${1:-0}; stubblocked=${2:-0}
  log "boot recovery: binder pool at $have/$BINDER_COUNT devices"
  if [ "$stubblocked" = "1" ]; then
    printf '%s\n' "boot recovery paused: binder pool blocked by a non-empty stub directory - manual cleanup needed" > "$ST/boot.degraded" 2>/dev/null
    return 0  # ALERT already logged; fleet start paused until human cleanup
  fi
  if [ "$have" -lt "$BINDER_COUNT" ]; then
    printf '%s\n' "boot recovery paused: binder pool incomplete ($have/$BINDER_COUNT) - fleet start paused" > "$ST/boot.degraded" 2>/dev/null
    log "WARN boot recovery: binder pool incomplete ($have/$BINDER_COUNT) - fleet start paused, retrying next cycle"
    return 0
  fi
  # Pool complete: the pause is over (even while the stagger below is
  # still draining), so a marker from an earlier paused cycle must go.
  rm -f "$ST/boot.degraded" 2>/dev/null
  # down-queue: managed instances whose container is not running.
  local psout down_list="" i c
  if ! psout=$(dw "$DW_FAST" ps -a --format '{{.Names}}|{{.State}}' 2>/dev/null); then
    log "WARN boot recovery: docker ps -a failed - retrying next cycle"
    return 0
  fi
  for i in $INSTANCES; do
    c="wd-$i"
    [ -f "$ST/$i.degraded" ] && continue
    if ! printf '%s\n' "$psout" | grep -qx "$c|running"; then
      down_list="$down_list $i"
    fi
  done
  down_list=$(printf '%s' "$down_list" | tr -s ' ' | sed 's/^ //;s/ $//')
  if [ -z "$down_list" ]; then
    touch "$ST/boot.done" 2>/dev/null
    log "boot recovery complete: fleet already running"
    return 0
  fi
  local started=0 failed=0 degraded_now=0 total=0 attempted=0 n
  for i in $down_list; do total=$((total+1)); done
  for i in $down_list; do
    c="wd-$i"
    # budget counts start INVOCATIONS (attempts), not successes: a failed
    # start must not let the next instance jump the stagger queue.
    if [ "$attempted" -ge "$BOOT_MAX_STARTS" ]; then
      continue  # over this cycle's budget: stays queued for next cycle
    fi
    attempted=$((attempted+1))
    n=$(note_event "$i.bootstart" 3600)
    if [ "$n" -gt "$BOOT_ATTEMPTS" ]; then
      printf '%s\n' "failed to start $n times during boot recovery - auto-recovery paused" > "$ST/$i.degraded"
      log "$i DEGRADED: failed to start $n times during boot recovery - auto-recovery paused (needs human)"
      degraded_now=$((degraded_now+1))
      continue
    fi
    if dw "$DW_ACTION" start "$c" >/dev/null 2>&1; then
      log "$i boot recovery: started ok"
      started=$((started+1))
    else
      log "$i boot recovery: start FAILED (attempt $n/$BOOT_ATTEMPTS)"
      failed=$((failed+1))
    fi
  done
  # queue drains when every down instance was started or degraded this run;
  # failures under budget stay queued for the next cycle.
  if [ $((started + degraded_now)) -ge "$total" ]; then
    touch "$ST/boot.done" 2>/dev/null
    log "boot recovery complete: started $started, degraded $degraded_now, failed-retry $failed"
  else
    log "boot recovery: started $started/$total this cycle (max $BOOT_MAX_STARTS/cycle), $failed failed-retry, $degraded_now degraded"
  fi
  return 0
}
# --- end v14 ---

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
  local _loop_restarted=0  # set when section 3 restarts screencap-loop.sh this cycle (section 4 reads it)
  # 1. container running? (2 strikes -> start; circuit-broken at 6 restarts/hour)
  # v13: dw-bounded — a hung `docker ps` (wedged daemon) fails after DW_FAST
  # seconds and follows the normal down/strike path instead of freezing the run.
  if ! dw "$DW_FAST" ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
    # v14: boot recovery owns container starts until it completes — starting
    # here would race the staggered queue and re-create the post-reboot herd.
    if boot_recovery_active; then
      return
    fi
    if [ "$(bump "$i.norun")" -ge 2 ]; then
      if n=$(cb_guard "$i" "$i.restart" 3600 6 "container"); then
        log "$i container down -> docker start (restart ${n}/6 this hour)"
        if dw "$DW_ACTION" start "$c" >/dev/null 2>&1; then log "$i started ok"; else log "$i start FAILED"; fi
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
    start=$(date -d "$(dw "$DW_FAST" inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null)" +%s 2>/dev/null || echo 0)
    now=$(date +%s)
    if [ "$start" -gt 0 ] && [ $((now - start)) -gt 900 ]; then
      if [ "$(bump "$i.stuck")" -ge 2 ]; then
        if n=$(cb_guard "$i" "$i.restart" 3600 6 "container"); then
          log "$i stuck booting (no marker after 15m) -> docker restart (${n}/6 this hour)"
          dw "$DW_ACTION" restart "$c" >/dev/null 2>&1
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
    local _sess_ok=0 _sess_unknown=0 _sess_out _sess_rc
    # v13: dw-bounded. A timed-out probe (exit 124) is UNKNOWN — wedged
    # docker or wedged exec path — not dead: skip recovery this cycle
    # instead of burning the session circuit budget or issuing a start
    # against a possibly-live session. A fast failure (rc != 0, != 124)
    # keeps the old dead-session semantics.
    _sess_out=$(dw "$DW_FAST" exec "$c" waydroid shell getprop sys.boot_completed 2>/dev/null)
    _sess_rc=$?
    if [ "$_sess_rc" -eq 124 ]; then
      _sess_unknown=1
      _probe_timeout_warn "$i" "session probe"
    elif [ "$_sess_rc" -eq 0 ] && printf '%s\n' "$_sess_out" | grep -q '^1$'; then
      _sess_ok=1
    fi
    if [ "$_sess_ok" = "1" ]; then
      rm -f "$ST/$i.sessionstart.ts" 2>/dev/null
    elif [ "$_sess_unknown" = "1" ]; then
      # Blind this cycle: do not declare the session dead, and do not let
      # section 6 auto-clear a DEGRADED marker while the probes are blind.
      healthy=0
      return
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
        if dw "$DW_ACTION" exec -d "$c" waydroid session start >/dev/null 2>&1; then
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
  local s hk _pg_out _pg_rc
  for s in screencap-loop.sh input-daemon.sh roblox-keepalive.sh; do
    # v13: dw-bounded. A timed-out pgrep (exit 124) is UNKNOWN, not dead —
    # do not issue a restart against a possibly-live helper and do not burn
    # the helper circuit budget; the next minute retries.
    _pg_out=$(dw "$DW_FAST" exec "$c" pgrep -f "$s" 2>/dev/null); _pg_rc=$?
    if [ "$_pg_rc" -eq 124 ]; then
      healthy=0
      _probe_timeout_warn "$i" "helper probe ($s)"
      continue
    fi
    if [ "$_pg_rc" -ne 0 ]; then
      healthy=0
      hk=$(printf '%s' "$s" | tr -c 'a-zA-Z0-9' '_')
      if [ ! -f "$base/$s" ]; then
        # The helper script was never installed into the data volume (e.g. a
        # slow first boot that missed the entrypoint's setup on an image
        # predating boot-complete.sh). Restarting it would fail silently, so
        # say so plainly and do NOT burn the circuit-breaker budget on a
        # certain failure. One WARN per day; the fix is a live-container push
        # (push-keepalive.sh / push-booted-watcher.sh / push-screencap.sh) or
        # a fresh instance from a current image.
        _hm="$ST/$i.helper-missing-$hk.$(date +%F)"
        if [ ! -f "$_hm" ]; then
          rm -f "$ST"/$i.helper-missing-$hk.2* 2>/dev/null
          touch "$_hm" 2>/dev/null
          log "$i WARN: $s missing from data volume - boot setup never installed it; cannot restart (push the script or recreate the instance)"
        fi
        continue
      fi
      if n=$(cb_guard "$i" "$i.helper-$hk" 600 8 "$s"); then
        log "$i $s dead -> restart (${n}/8 per 10m)"
        # v13: intentionally bare `docker` (not dw-bounded) — the exec client
        # stays attached to the looping helper for its whole life; a timeout
        # here would kill the helper it just started. Liveness is re-verified
        # by the bounded pgrep probe next minute.
        docker exec "$c" nohup waydroid shell sh "/data/media/0/jerusalem/$s" >/dev/null 2>&1 &
        # missing-frame-guard: section 4's missing-file branch must not
        # double-restart a loop section 3 just issued a restart for.
        [ "$s" = "screencap-loop.sh" ] && _loop_restarted=1
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
  # 3c. Roblox install-failure surfacing (v15): boot-complete.sh writes
  #     roblox_install_failed (reason on first line) when the one-shot pm
  #     install fails, times out, or the APKs are missing from the image.
  #     Mirror it as the instance degraded marker so the dashboard "needs
  #     attention" badge shows it. Not routed through cb_guard — this is
  #     surfacing, not auto-recovery (a failed install needs a human);
  #     a pre-existing circuit-breaker marker is never clobbered; while
  #     present the instance counts as unhealthy, so the auto-clear in
  #     section 6 waits for boot-complete to clear its own marker on a
  #     later successful run.
  if [ -f "$base/roblox_install_failed" ]; then
    healthy=0
    if [ ! -f "$ST/$i.degraded" ]; then
      local _ir
      _ir=$(head -c 160 "$base/roblox_install_failed" 2>/dev/null | head -1)
      printf 'Roblox install failed: %s\n' "${_ir:-unknown error}" > "$ST/$i.degraded" 2>/dev/null
      log "$i DEGRADED: Roblox install failed (${_ir:-unknown error}) - needs human"
    fi
  fi
  # 4. stream fresh? (stale >30s -> restart loop)
  local png="$base/live.png"
  if [ -f "$png" ]; then
    local age=$(( $(date +%s) - $(stat -c %Y "$png" 2>/dev/null || echo 0) ))
    if [ "$age" -gt 30 ]; then
      healthy=0
      if [ ! -f "$base/screencap-loop.sh" ]; then
        # Same missing-script case as section 3: restarting the loop would
        # fail silently. Distinct WARN, no circuit-budget burn.
        _hm="$ST/$i.helper-missing-screencap_loop_sh.$(date +%F)"
        if [ ! -f "$_hm" ]; then
          rm -f "$ST"/$i.helper-missing-screencap_loop_sh.2* 2>/dev/null
          touch "$_hm" 2>/dev/null
          log "$i WARN: screencap-loop.sh missing from data volume - boot setup never installed it; cannot restart stale stream (push the script or recreate the instance)"
        fi
      elif n=$(cb_guard "$i" "$i.staleloop" 3600 10 "screencap-loop"); then
        log "$i screencap stale (${age}s) -> restart loop (${n}/10 per hour)"
        dw "$DW_FAST" exec "$c" pkill -f screencap-loop.sh 2>/dev/null
        # v13: intentionally bare `docker` (not dw-bounded) — same reason as
        # the helper restart above: the exec client stays attached to the
        # looping screencap script for its whole life.
        docker exec "$c" nohup waydroid shell sh /data/media/0/jerusalem/screencap-loop.sh >/dev/null 2>&1 &
      fi
    fi
  elif [ -f "$base/booted" ]; then
    # 4b. frame MISSING entirely (missing-frame-guard): live.png was never
    #     written while the loop is alive, i.e. screencap fails every cycle
    #     (no display yet, broken surface). The old code only handled a
    #     STALE file, so this case never recovered: the viewer spun on
    #     MJPEG reconnects forever with no watchdog repair and no DEGRADED
    #     badge. The loop installs live.png by atomic mv, so a missing file
    #     is never a mid-write transient — it means no capture ever
    #     succeeded. Booted gate: a first-boot instance legitimately has no
    #     frame yet. A dead loop is section 3's job (and a loop restarted
    #     by section 3 this cycle is skipped via _loop_restarted, so one
    #     cycle never double-restarts). Shares the staleloop budget with
    #     the stale branch, so a flapping loop can't burn both. A timed-out
    #     pgrep (124) is UNKNOWN -> skip, never burn.
    healthy=0
    if [ ! -f "$base/screencap-loop.sh" ]; then
      # Same missing-script case as the stale branch (shared per-day WARN
      # marker: one WARN per day for the script, whichever branch notices).
      _hm="$ST/$i.helper-missing-screencap_loop_sh.$(date +%F)"
      if [ ! -f "$_hm" ]; then
        rm -f "$ST"/$i.helper-missing-screencap_loop_sh.2* 2>/dev/null
        touch "$_hm" 2>/dev/null
        log "$i WARN: screencap-loop.sh missing from data volume - boot setup never installed it; cannot restart missing stream (push the script or recreate the instance)"
      fi
    elif [ "$_loop_restarted" = "1" ]; then
      : # section 3 restarted the loop this cycle; next minute judges it
    else
      _pg_out=$(dw "$DW_FAST" exec "$c" pgrep -f screencap-loop.sh 2>/dev/null); _pg_rc=$?
      if [ "$_pg_rc" -eq 124 ]; then
        _probe_timeout_warn "$i" "helper probe (screencap-loop.sh)"
      elif [ "$_pg_rc" -ne 0 ]; then
        : # loop dead: section 3 owns the restart (or just issued it)
      elif n=$(cb_guard "$i" "$i.staleloop" 3600 10 "screencap-loop"); then
        log "$i screencap missing (no live.png) -> restart loop (${n}/10 per hour)"
        dw "$DW_FAST" exec "$c" pkill -f screencap-loop.sh 2>/dev/null
        # v13: intentionally bare `docker` (not dw-bounded) — same reason as
        # the helper restart above.
        docker exec "$c" nohup waydroid shell sh /data/media/0/jerusalem/screencap-loop.sh >/dev/null 2>&1 &
      fi
    fi
  fi
  # 5. Roblox client version-pin guard: login tap coordinates and the
  #    device-integrity exposure are tuned to PINNED_ROBLOX. Drift (auto or
  #    forced update) gets ONE loud ALERT per drifted version; there is no
  #    auto-remediation — a force-updated client needs a human decision.
  #    Drift also writes a <instance>.roblox_drift marker (reason on the
  #    first line, same convention as .degraded markers) so the manager can
  #    surface it as a dashboard badge. The marker's lifecycle is independent
  #    of the circuit-breaker markers: it is removed only when the client is
  #    observed back on the pinned build.
  if [ -f "$base/booted" ]; then
    # v13: dw-bounded — a hung dumpsys yields empty output and takes the
    # existing silent-skip path (missing client is not version drift).
    _ver=$(dw "$DW_FAST" exec "$c" waydroid shell dumpsys package com.roblox.client 2>/dev/null \
      | grep -m1 '^ *versionName=' | cut -d= -f2 | tr -d '[:space:]')
    if [ -n "$_ver" ]; then
      _vf="$ST/$i.robloxver"
      _dm="$ST/$i.roblox_drift"
      if [ "$_ver" != "$PINNED_ROBLOX" ]; then
        printf '%s\n' "Roblox client $_ver installed (pinned $PINNED_ROBLOX) - login coords + integrity assumptions tuned to pinned build; no auto-remediation" > "$_dm" 2>/dev/null
        if [ ! -f "$_vf" ] || [ "$(cat "$_vf" 2>/dev/null)" != "$_ver" ]; then
          printf '%s\n' "$_ver" > "$_vf" 2>/dev/null
          log "ALERT $i Roblox client version DRIFT: installed $_ver (pinned $PINNED_ROBLOX) - login coords + integrity assumptions tuned to pinned build; no auto-remediation, needs human"
        fi
      elif [ -f "$_vf" ]; then
        rm -f "$_vf" "$_dm" 2>/dev/null
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

# --- v16: instance-name gate (single choke point, before both loops) ---
# INSTANCES comes from instances.json keys (manager state) or DF_INSTANCES.
# The manager only ever creates names matching ^[A-Za-z0-9_-]{1,32}$ (see
# valid_instance_name), but the watchdog reads the file independently — a
# corrupted record or hand-edited file could carry a key like ../../tmp/x,
# and check_one builds marker paths ($ST/$i.degraded, $ST/$i.robloxver) and
# container names (wd-$i) straight from the key, so an unvalidated key
# writes marker files outside $ST and hands docker nonsense names. Keep
# only manager-valid names; one WARN line per cycle names the skipped keys
# (first 5 + count, so a hostile file can't flood the 2MB-trimmed log).
# --- V16-NAME-GATE-START ---
_gate_valid=""
_gate_skipped=""
_gate_nskip=0
for _gate_n in $INSTANCES; do
  if [[ "$_gate_n" =~ ^[A-Za-z0-9_-]{1,32}$ ]]; then
    _gate_valid="${_gate_valid:+$_gate_valid }$_gate_n"
  else
    _gate_nskip=$((_gate_nskip + 1))
    if [ "$_gate_nskip" -le 5 ]; then
      _gate_skipped="${_gate_skipped:+$_gate_skipped }$_gate_n"
    fi
  fi
done
if [ "$_gate_nskip" -gt 0 ]; then
  log "WARN watchdog: skipping $_gate_nskip invalid instance name(s): $_gate_skipped"
fi
INSTANCES="$_gate_valid"
# --- V16-NAME-GATE-END ---

# --- v15: docker-daemon guard (section 0b) ---
# Probes the daemon itself before ANY docker-dependent recovery. A down
# daemon must not be mistaken for dead containers (see the header note):
# while the probe fails, boot recovery, the per-instance loop, and the disk
# auto-prune are skipped, and after 2 consecutive failures dockerd is
# restarted (2/hour cb_guard budget, same shape as the section-7 manager
# self-check). The probe uses dw() so a wedged daemon fails after DW_FAST
# instead of hanging the run; a timeout (124) counts as down, not unknown
# — instance recovery cannot run against a daemon that will not answer.
DOCKER_DOWN=0
if ! dw "$DW_FAST" info >/dev/null 2>&1; then
  DOCKER_DOWN=1
  _dstrikes=$(bump "docker.daemondown")
  log "docker daemon unreachable (strike $_dstrikes/2) - docker-dependent recovery skipped this cycle"
  if [ "$_dstrikes" -ge 2 ]; then
    if _dn=$(cb_guard "docker" "docker.restart" 3600 2 "docker daemon"); then
      log "docker daemon unreachable twice -> systemctl restart docker (${_dn}/2 this hour)"
      if systemctl restart docker 2>/dev/null; then log "docker restarted ok"; else log "docker restart FAILED"; fi
      reset "docker.daemondown"
    fi
  fi
else
  reset "docker.daemondown"
  if [ -f "$ST/docker.degraded" ]; then
    rm -f "$ST/docker.degraded" "$ST"/docker.restart.ev 2>/dev/null
    log "docker daemon recovered from DEGRADED (probe healthy) - auto-recovery resumed"
  fi
fi

if [ "$DOCKER_DOWN" = 0 ]; then
# --- v14: host-boot fleet recovery (section 0) ---
# Restores the binderfs pool and stagger-starts stopped instances after a
# host reboot, before the per-instance loop. While active, check_one's
# container-down branch defers to it (see the v14 guard in check_one).
boot_recovery

# per-instance checks in parallel so the minute-cron stays fast.
# Belt-and-braces (v11): each backgrounded check clears the EXIT trap first.
# Verified on this bash that `&` subshells never run the inherited EXIT trap
# (so even without this they could not release the main run's lock early),
# but an explicit `exit` inside a future check_one must never be able to
# either — the pid-checked _release_lock is the second line of defense.
for i in $INSTANCES; do ( trap - EXIT; check_one "$i" ) & done
wait
fi # DOCKER_DOWN = 0 (v15 gate: no docker-dependent recovery while blind)

# 7. manager service self-check (wedged-but-alive is invisible to systemd):
#    probe the dashboard root; the probe requires a COMPLETE body (curl
#    exit 0) AND 2xx/3xx status — a 2xx with a truncated body (starved
#    manager, fast TTFB then mid-body close) is a failed probe, not healthy.
#    2 consecutive failures -> systemctl start (service down) or restart
#    (service up but not answering). Circuit-broken: >2 manager
#    restarts/hour -> global manager.degraded marker, auto-recovery pauses
#    until a complete+healthy probe passes again (auto-clears).
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
  # v12: fetch the body (not /dev/null) so a truncated transfer is visible.
  _pb="$ST/mgr.probe.body"; _pc="$ST/mgr.probe.code"
  rm -f "$_pb" "$_pc"
  "$CURL_BIN" -s -o "$_pb" -w '%{http_code}' --max-time 8 "$MGR_URL" >"$_pc" 2>/dev/null
  _curlrc=$?
  _code=$(cat "$_pc" 2>/dev/null); [ -z "$_code" ] && _code="000"
  rm -f "$_pb" "$_pc"
  case "$_curlrc:$_code" in
    0:2*|0:3*)
      reset "mgr.httpdown"
      if [ -f "$ST/manager.degraded" ]; then
        rm -f "$ST/manager.degraded" "$ST"/mgr.*.ev 2>/dev/null
        log "manager recovered from DEGRADED (HTTP $_code healthy) - auto-recovery resumed"
      fi
      ;;
    *)
      _strikes=$(bump "mgr.httpdown")
      _trunc=0
      case "$_code" in 2*|3*) [ "$_curlrc" -ne 0 ] && _trunc=1 ;; esac
      if [ "$_trunc" -eq 1 ]; then
        log "manager HTTP $_code but body truncated (curl rc $_curlrc, strike $_strikes/2)"
      else
        log "manager HTTP probe failed (code $_code, strike $_strikes/2)"
      fi
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
      fi
      ;;
  esac
fi

# 8. disk pressure: bounded SAFE auto-recovery at >=90%, then alert.
#    Only ever prunes dangling images + build cache. NEVER volume prune or
#    `image prune -a` — instance data lives in named volumes + the instance
#    image. Budgeted: >2 prunes/hour without relief -> disk.degraded marker,
#    auto-prune pauses (alert-only) until usage drops below 90%.
#    v15: the prune attempt itself is skipped while the docker daemon is
#    unreachable (DOCKER_DOWN=1) — a prune against a dead daemon fails and
#    would burn this budget for nothing; the alert WARN below still fires.
disk_use() { df /var/lib/docker 2>/dev/null | awk 'NR==2{sub(/%/,"",$5); print $5}'; }
usep=$(disk_use)
if [ -n "$usep" ] && [ "$usep" -lt 90 ] && [ -f "$ST/disk.degraded" ]; then
  rm -f "$ST/disk.degraded" "$ST"/disk.prune.ev 2>/dev/null
  log "disk pressure relieved (${usep}%) - auto-prune resumed"
fi
if [ -n "$usep" ] && [ "$usep" -ge 90 ]; then
  if [ -f "$ST/disk.degraded" ]; then
    log "WARN disk ${usep}% on /var/lib/docker - auto-prune paused (budget exhausted, needs human)"
  elif [ "$DOCKER_DOWN" = 1 ]; then
    # v15: a prune against a dead daemon fails and would burn the disk
    # budget above for nothing — skip the attempt (the alert WARN below
    # still fires, so the pressure itself is never hidden).
    log "WARN disk ${usep}% on /var/lib/docker - auto-prune skipped (docker daemon unreachable)"
  else
    _pn=$(note_event "disk.prune" 3600)
    if [ "$_pn" -gt 2 ]; then
      printf '%s\n' "disk auto-prune ran ${_pn} times in last 3600s without relieving pressure - auto-prune paused" > "$ST/disk.degraded"
      log "disk DEGRADED: auto-prune ran ${_pn}x this hour, still ${usep}% - paused (needs human)"
    else
      _before="$usep"
      # v13: dw-bounded with the slow budget — prunes can legitimately take a
      # while on a big cache; a hung daemon still fails after DW_SLOW.
      _freed=$(dw "$DW_SLOW" image prune -f 2>/dev/null | grep -i 'total reclaimed' | head -1)
      dw "$DW_SLOW" builder prune -f >/dev/null 2>&1
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
