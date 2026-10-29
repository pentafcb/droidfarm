#!/bin/bash
# backup-state.sh — daily Jerusalem 1.0 manager state backup.
#
# Runs on the VPS from cron (03:17, root). Replaces the 2026-10-06 backup
# cron's explicit file list, which silently skipped every state file added
# after it was written (roblox_login_results.json from 2026-10-07 and the
# batch_jobs/ job journals from 2026-10-11 were never archived). The state
# set is defined HERE, once, in STATE_FILES: a new state file gets added
# here and flows into every future backup — no cron-line edits ever again.
#
# Archive: $JERUSALEM_BACKUPS/jerusalem-state-YYYYMMDD-HHMMSS.tar.gz,
# 600 perms, written atomically (tmp + rename), RETENTION_DAYS of history.
# Self-verifying: the finished archive is re-opened, every member listed,
# every .json member JSON-validated, and the archived set is checked
# against the state set before old archives are pruned.
# Logs carry file NAMES, counts and sizes only — never file contents.
#
# Env overrides (also used by the unit tests):
#   JERUSALEM_DATA        state dir (default /root/jerusalem)
#   JERUSALEM_BACKUPS     archive dir (default /root/jerusalem-backups)
#   JERUSALEM_BACKUP_LOG  log file (default /var/log/jerusalem-backup.log)
#   RETENTION_DAYS        keep this many days (default 7)
#   MIN_FREE_MB           require this much free space (default 100)
#
# Status marker: $DATA/last-backup.json, written on EVERY run (success and
# failure) — the dashboard's GET /api/backup-status renders it in the fleet
# bar, and verify-live.sh --ssh probes it. Without the marker, a dead 03:17
# cron or a failed backup is invisible until a restore is needed. The
# marker is atomic (tmp + rename), 600 perms, and carries names/counts/
# sizes only — never file contents.
set -u

DATA="${JERUSALEM_DATA:-/root/jerusalem}"
BKDIR="${JERUSALEM_BACKUPS:-/root/jerusalem-backups}"
LOGF="${JERUSALEM_BACKUP_LOG:-/var/log/jerusalem-backup.log}"
RETENTION="${RETENTION_DAYS:-7}"
MINFREE="${MIN_FREE_MB:-100}"
MARKER="$DATA/last-backup.json"
_backup_done=0
_fail_reason="unknown"

# The canonical state set. Add new manager state files here and they are
# archived by every future run. Paths are relative to $DATA (no leading /,
# no .. — the list is hardcoded, never from user input).
STATE_FILES="instances.json providers.json tokens.json roblox_accounts.json roblox_login_results.json static_join_token"

ts() { date -u +%FT%TZ; }
ts_epoch() { date -u +%s; }
say()  { echo "[backup-state] $*"; }
warn() { echo "[backup-state] WARNING: $*" >&2; }
fail() { _fail_reason="$1"; echo "[backup-state] FAIL: $*" >&2; return 1; }
logline() { { echo "$(ts) $*" >> "$LOGF"; } 2>/dev/null || warn "cannot write log $LOGF"; }

# --- status marker helpers (never print contents, never raise) ---
_write_marker() { # $1 = full JSON body (server-generated fields only)
    local mtmp="$DATA/.tmp-last-backup.json"
    printf '%s' "$1" > "$mtmp" 2>/dev/null \
        && chmod 600 "$mtmp" 2>/dev/null \
        && mv -f "$mtmp" "$MARKER" 2>/dev/null
}
marker_ok() { # $1 archive basename, $2 bytes, $3 files, $4 pruned
    _write_marker "{\"ts\":\"$(ts)\",\"ts_epoch\":$(ts_epoch),\"ok\":true,\"archive\":\"$1\",\"bytes\":$2,\"files\":$3,\"pruned\":$4}"
}
marker_fail() {
    _write_marker "{\"ts\":\"$(ts)\",\"ts_epoch\":$(ts_epoch),\"ok\":false,\"error\":\"$_fail_reason\"}"
}
finish_trap() {
    rm -f "${tmp:-}"
    [ "$_backup_done" = 1 ] || marker_fail
}
trap finish_trap EXIT

[ -d "$DATA" ] || { fail "state dir missing: $DATA"; exit 1; }
mkdir -p "$BKDIR" || { fail "cannot create $BKDIR"; exit 1; }

# --- build the include list; missing files WARN (never silent), never abort ---
include=()
missing=()
for f in $STATE_FILES; do
    if [ -f "$DATA/$f" ]; then
        include+=("$f")
    else
        missing+=("$f")
        warn "state file missing, not archived: $f"
    fi
done
# Fleet batch-job journals: server-generated names, validated at write time
# by the manager (_journal_path only allows hex job ids + .json).
if [ -d "$DATA/batch_jobs" ]; then
    bj=0
    for j in "$DATA/batch_jobs"/*.json; do
        [ -e "$j" ] || continue
        include+=("batch_jobs/$(basename "$j")")
        bj=$((bj + 1))
    done
    [ "$bj" = 0 ] && warn "batch_jobs/ present but holds no journals"
else
    warn "batch_jobs/ absent (no fleet jobs journaled yet), skipped"
fi
[ "${#include[@]}" = 0 ] && { fail "no state files found — refusing to archive nothing"; exit 1; }

# --- free-space pre-check ---
avail="$(df -m --output=avail "$BKDIR" 2>/dev/null | tail -1 | tr -d ' ')"
if [ -n "$avail" ] && [ "$avail" -lt "$MINFREE" ] 2>/dev/null; then
    fail "only ${avail}MB free in $BKDIR (need ${MINFREE}MB) — skipping backup"
    exit 1
fi

# --- atomic write ---
name="jerusalem-state-$(date -u +%Y%m%d-%H%M%S).tar.gz"
tmp="$BKDIR/.tmp-$name"
final="$BKDIR/$name"
tar -czf "$tmp" -C "$DATA" "${include[@]}" 2>/dev/null \
    || { fail "tar failed"; exit 1; }
chmod 600 "$tmp"

# --- self-verify: re-open, list, JSON-validate, set-check ---
members="$(tar -tzf "$tmp" 2>/dev/null)" || { fail "archive unreadable after write"; exit 1; }
[ -n "$members" ] || { fail "archive holds zero members"; exit 1; }
if command -v python3 >/dev/null 2>&1; then
    vdir="$(mktemp -d)"
    tar -xzf "$tmp" -C "$vdir" 2>/dev/null || { rm -rf "$vdir"; fail "archive extract failed"; exit 1; }
    while IFS= read -r m; do
        case "$m" in *.json)
            python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$vdir/$m" 2>/dev/null \
                || { rm -rf "$vdir"; fail "member is not valid JSON: $m"; exit 1; } ;;
        esac
    done <<< "$members"
    rm -rf "$vdir"
else
    warn "python3 missing — skipping JSON validation of the archive"
fi
for f in $STATE_FILES; do
    [ -f "$DATA/$f" ] || continue          # already warned above
    printf '%s\n' "$members" | grep -qx "$f" \
        || { fail "self-verify: $f archived on disk but missing from the tar"; exit 1; }
done

mv -f "$tmp" "$final"
bytes="$(stat -c%s "$final" 2>/dev/null || stat -f%z "$final" 2>/dev/null || echo 0)"

# --- retention prune (only after a verified write) ---
pruned=0
if [ "$RETENTION" -gt 0 ] 2>/dev/null; then
    while IFS= read -r old; do
        rm -f "$old" && pruned=$((pruned + 1))
    done < <(find "$BKDIR" -maxdepth 1 -name 'jerusalem-state-*.tar.gz' \
        -mtime "+$((RETENTION - 1))" ! -path "$final" 2>/dev/null)
fi

marker_ok "$(basename "$final")" "$bytes" "${#include[@]}" "$pruned"
_backup_done=1
say "ok: $final files=${#include[@]} bytes=$bytes pruned=$pruned"
logline "ok files=${#include[@]} bytes=$bytes pruned=$pruned archive=$(basename "$final")"
exit 0
