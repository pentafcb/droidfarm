# --- LOG-TRIMMER (watchdog section 10) ---
# Drop-in for the END of df-watchdog.sh (before `exit 0`).
# Purpose: Docker's json-file driver never rotates container logs by default.
# Each instance container's *-json.log captures ALL Waydroid session stdout
# (LXC/Android boot + runtime chatter from `waydroid container start` and
# `waydroid session start`, which inherit the entrypoint's stdout) and can
# grow to tens of GB — 69G was observed 2026-10-08, starving the disk and
# plausibly preventing new Android boots.
#
# Once per minute-cron run, any *-json.log over 500MB is trimmed IN PLACE to
# the last 100MB with copytruncate semantics:
#   tail -c 100M log > log.tmp ; cat log.tmp > log
# `cat > log` truncates and rewrites the SAME inode, so dockerd's open file
# descriptor stays valid and logging continues uninterrupted. Writes past the
# new EOF become sparse holes (no disk blocks consumed), so disk usage
# actually drops. The last 100MB of log is always preserved for debugging
# failed boots. Instance data volumes are never touched.
#
# Test overrides: DF_LOG_TRIM_MAX (bytes, default 500MB),
#                 DF_LOG_TRIM_KEEP (bytes, default 100MB).

TRIM_MAX="${DF_LOG_TRIM_MAX:-524288000}"
TRIM_KEEP="${DF_LOG_TRIM_KEEP:-104857600}"
for _jl in /var/lib/docker/containers/*/*-json.log; do
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
unset _jl _js _t TRIM_MAX TRIM_KEEP
# --- END LOG-TRIMMER ---
