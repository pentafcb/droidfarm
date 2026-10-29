#!/bin/bash
# repoint-backup-cron.sh — one-time post-rename fix for the 03:17 daily
# state-backup cron.
#
# The daily backup cron was created 2026-10-06 targeting the manager's state
# dir; the 2026-10-08 rename moved that dir from the legacy pre-rename
# location to /root/jerusalem, but the cron was never repointed — so the
# backups have been silently archiving the wrong directory ever since.
#
# Run once as root on the main VPS, after apply-all (the script is staged to
# /root/jerusalem/repoint-backup-cron.sh) or via standalone scp. It:
#   - searches root's personal crontab AND /etc/cron.d/* for lines that look
#     like the state backup (contain "backup", case-insensitive) AND reference
#     the legacy pre-rename manager dir;
#   - rewrites only those lines, replacing the legacy dir with
#     /root/jerusalem (word-boundary aware: /root/jerusalem-deploy and other
#     lookalikes are NOT touched);
#   - leaves non-backup lines alone (out of scope — verify-live's --ssh cron
#     check only covers backup lines);
#   - is idempotent: re-running after a successful repoint is a no-op.
#
# Exit 0: repointed / already repointed (no-op) / no backup cron found
#         (warning printed — recreate it per DEPLOY.md).
# Exit 2: an actual error (unreadable/unwritable cron source).
# Never prints secrets: cron lines carry paths and schedules only.
#
# Test env overrides (no-ops in production):
#   BACKUP_CROND_DIR  cron.d directory to scan (default /etc/cron.d)
#   BACKUP_CRONTAB    crontab binary to use (default crontab)
set -u

CROND_DIR="${BACKUP_CROND_DIR:-/etc/cron.d}"
CRONTAB_BIN="${BACKUP_CRONTAB:-crontab}"
NEW_DIR="/root/jerusalem"
# legacy-guard: the pre-rename manager dir; this script detects it in backup
# cron lines and rewrites it to NEW_DIR. Kept in a variable so the rewrite
# patterns below stay in sync.
LEGACY_DIR="/root/droidfarm" # legacy-guard: pre-rename manager dir (do not "fix" this line)

errors=0
changed=0

log()  { echo "[repoint-backup-cron] $*"; }
warn() { echo "[repoint-backup-cron] WARNING: $*" >&2; }

# rewrite_legacy_line <line>: print the line with the legacy pre-rename dir
# replaced by NEW_DIR. Word-boundary aware:
#   /root/droidfarm/ (legacy) -> /root/jerusalem/      (dir as prefix)
#   /root/droidfarm (legacy, bare path at end/space/quote/;) -> /root/jerusalem
#   /root/droidfarm-deploy (legacy lookalike) -> UNTOUCHED (the "-" is excluded)
rewrite_legacy_line() { # legacy-guard: rewrites the pre-rename dir in backup cron lines
    printf '%s\n' "$1" \
        | sed -e "s|${LEGACY_DIR}/|${NEW_DIR}/|g" \
              -e "s|${LEGACY_DIR}\([^[:alnum:]_/-]\)|${NEW_DIR}\1|g" \
              -e "s|${LEGACY_DIR}\$|${NEW_DIR}|"
}

is_backup_line() {
    case "$1" in
        *[Bb][Aa][Cc][Kk][Uu][Pp]*) return 0 ;;
        *) return 1 ;;
    esac
}

# needs_repoint <line>: true when the line carries a REWRITABLE occurrence of
# the legacy pre-rename dir (i.e. the rewrite actually changes the line).
# Comparing against the rewrite output (instead of a substring match) keeps
# legacy lookalikes like /root/droidfarm-deploy out of the repoint path: the
# rewrite leaves them byte-identical, so they are never counted or flagged.
needs_repoint() { # legacy-guard: detects rewritable pre-rename dir occurrences
    [ "$(rewrite_legacy_line "$1")" != "$1" ]
}

# process_cron_file <file>: rewrite legacy backup lines in place.
# Prints "path: N line(s) repointed" when it changes something.
process_cron_file() {
    local f="$1" tmp n_old line
    [ -f "$f" ] || return 0
    [ -r "$f" ] || { warn "cannot read $f"; errors=$((errors+1)); return 0; }
    tmp="$(mktemp)" || { warn "mktemp failed"; errors=$((errors+1)); return 0; }
    n_old=0
    while IFS= read -r line || [ -n "$line" ]; do
        if is_backup_line "$line" && needs_repoint "$line"; then
            new_line="$(rewrite_legacy_line "$line")"
            printf '%s\n' "$new_line" >> "$tmp"
            log "$f: repointed: $line"
            log "$f:        -> $new_line"
            n_old=$((n_old+1))
        else
            printf '%s\n' "$line" >> "$tmp"
        fi
    done < "$f"
    if [ "$n_old" -gt 0 ]; then
        # preserve mode/owner: copy over the original instead of mv
        if cat "$tmp" > "$f" 2>/dev/null; then
            changed=$((changed+n_old))
        else
            warn "cannot write $f (need root?)"
            errors=$((errors+1))
        fi
    fi
    rm -f "$tmp"
    return 0
}

# --- 1. /etc/cron.d/* files ---
if [ -d "$CROND_DIR" ]; then
    for f in "$CROND_DIR"/*; do
        [ -f "$f" ] || continue
        process_cron_file "$f"
    done
else
    warn "cron.d dir missing: $CROND_DIR (skipping)"
fi

# --- 2. root's personal crontab ---
if command -v "$CRONTAB_BIN" >/dev/null 2>&1; then
    cur="$("$CRONTAB_BIN" -l 2>/dev/null || true)"
    if [ -n "$cur" ]; then
        tmp="$(mktemp)" || { warn "mktemp failed"; errors=$((errors+1)); }
        if [ -n "${tmp:-}" ]; then
            n=0
            # process substitution (not a pipe): the while loop must run in
            # this shell so $n survives. printf, not a heredoc: an unquoted
            # heredoc would expand $(...) and backticks inside cron lines.
            while IFS= read -r line || [ -n "$line" ]; do
                if is_backup_line "$line" && needs_repoint "$line"; then
                    new_line="$(rewrite_legacy_line "$line")"
                    printf '%s\n' "$new_line" >> "$tmp"
                    log "crontab: repointed: $line"
                    log "crontab:        -> $new_line"
                    n=$((n+1))
                else
                    printf '%s\n' "$line" >> "$tmp"
                fi
            done < <(printf '%s\n' "$cur")
            if [ "$n" -gt 0 ]; then
                if "$CRONTAB_BIN" "$tmp" 2>/dev/null; then
                    changed=$((changed+n))
                else
                    warn "crontab install failed"
                    errors=$((errors+1))
                fi
            fi
            rm -f "$tmp"
        fi
    fi
else
    warn "no $CRONTAB_BIN binary (skipping personal crontab)"
fi

# --- 3. self-verify: no legacy backup lines may remain ---
leftover=0
scan_leftover() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        if is_backup_line "$line" && needs_repoint "$line"; then
            warn "leftover legacy backup line: $line" # legacy-guard: self-verify message
            leftover=$((leftover+1))
        fi
    done
}
if [ -d "$CROND_DIR" ]; then
    for f in "$CROND_DIR"/*; do
        [ -f "$f" ] || continue
        scan_leftover < "$f"
    done
fi
if command -v "$CRONTAB_BIN" >/dev/null 2>&1; then
    "$CRONTAB_BIN" -l 2>/dev/null | scan_leftover || true
fi
if [ "$leftover" -gt 0 ]; then
    warn "$leftover legacy backup line(s) still present after rewrite"
    errors=$((errors+1))
fi

if [ "$errors" -gt 0 ]; then
    warn "finished with $errors error(s)"
    exit 2
fi
if [ "$changed" -gt 0 ]; then
    log "done: $changed backup cron line(s) repointed to $NEW_DIR"
else
    # Distinguish "already fine" from "nothing to fix because there is no
    # backup cron at all" — the latter needs a human to recreate it.
    found_any=0
    if [ -d "$CROND_DIR" ]; then
        for f in "$CROND_DIR"/*; do
            [ -f "$f" ] || continue
            while IFS= read -r line || [ -n "$line" ]; do
                is_backup_line "$line" && found_any=1
            done < "$f"
        done
    fi
    if [ "$found_any" -eq 0 ] && command -v "$CRONTAB_BIN" >/dev/null 2>&1; then
        "$CRONTAB_BIN" -l 2>/dev/null | while IFS= read -r line || [ -n "$line" ]; do
            is_backup_line "$line" && { echo 1; break; }
        done | grep -q 1 && found_any=1
    fi
    if [ "$found_any" -eq 1 ]; then
        log "done: no legacy backup lines found — already repointed (no-op)"
    else
        warn "no backup cron line found anywhere — nothing repointed; recreate the 03:17 daily backup per DEPLOY.md"
    fi
fi
exit 0
