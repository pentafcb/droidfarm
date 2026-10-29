#!/bin/bash
# finish-deploy.sh — run ALL post-apply-all manual steps on the main VPS as root.
#
# Run ON THE MAIN VPS as root AFTER pending-deploy/apply-all/deploy-all.sh
# completes successfully:
#   bash /root/jerusalem/finish-deploy.sh
#
# Steps, in order (aborts on the first failure; every step is idempotent, so
# re-running this script is always safe):
#   1. push-keepalive.sh      — keepalive v2 into every booted wd-* container
#   2. push-booted-watcher.sh — booted-watcher loop into every wd-* container
#   3. push-screencap.sh      — adaptive screencap loop into every wd-* container
#   4. push-optimizer.sh      — canonical android-optimize.sh into every booted
#                              wd-* container, run once via waydroid shell
#   5. repoint-backup-cron.sh — 03:17 backup cron: legacy dir -> /root/jerusalem
#   6. cron-line switch       — replace old backup cron line(s) with the
#                              canonical backup-state.sh line, in root's
#                              personal crontab AND /etc/cron.d (no-op when
#                              already switched; fails closed when no backup
#                              cron exists — a backup command is never fabricated)
#   7. backup run + drill     — run backup-state.sh once, then verify-backup.sh
#                              the newest archive (proves the new cron line works)
#
# Exits: 0 = every step ok; 1 = a step failed (the summary names it).
# Logs carry step names and counts only — never file contents, credentials,
# or tokens.
#
# Env overrides (also used by the unit tests):
#   FD_DF_DIR         staged dir (default /root/jerusalem)
#   FD_CRONTAB_BIN    crontab binary (default crontab)
#   FD_CROND_DIR      cron.d directory scanned by the cron-line switch
#   FD_WATCHDOG_DST   watchdog install path (default /usr/local/bin/df-watchdog.sh)
#                     (default /etc/cron.d)
#   FD_SKIP_ROOT_CHECK=1  skip the "must run as root" check (tests)
#   JERUSALEM_BACKUPS / JERUSALEM_BACKUP_LOG / JERUSALEM_DATA / RETENTION_DAYS /
#   MIN_FREE_MB       passed through to backup-state.sh
set -u

DF_DIR="${FD_DF_DIR:-/root/jerusalem}"
CRONTAB_BIN="${FD_CRONTAB_BIN:-crontab}"
CROND_DIR="${FD_CROND_DIR:-/etc/cron.d}"

log()  { echo "[finish-deploy] $*"; }
step() { echo "[finish-deploy] ==> step $1: $2"; }
ok()   { echo "[finish-deploy] ok: $*"; }
fail() { echo "[finish-deploy] FAIL: $*" >&2; }

# --- step 0: preflight -------------------------------------------------------
# Every file apply-all stages must be present: a partial stage must never
# reach the live containers half-applied.
STAGED="h264_stream.py app.py index.html agent.py df-watchdog.sh roblox-keepalive.sh booted-watcher.sh repoint-backup-cron.sh screencap-loop.sh push-screencap.sh backup-state.sh verify-backup.sh push-keepalive.sh push-booted-watcher.sh android-optimize.sh push-optimizer.sh install-wing.sh install-wing.bat"

# staged_path: the real on-VPS location of a staged file, mirroring
# vps-apply.sh target_for (index.html -> static/, the three wing files
# -> wing/, df-watchdog.sh -> /usr/local/bin).
staged_path() {
    case "$1" in
        index.html)     echo "$DF_DIR/static/index.html" ;;
        agent.py|install-wing.sh|install-wing.bat) echo "$DF_DIR/wing/$1" ;;
        df-watchdog.sh) echo "${FD_WATCHDOG_DST:-/usr/local/bin/df-watchdog.sh}" ;;
        *)              echo "$DF_DIR/$1" ;;
    esac
}

preflight() {
    if [ "${FD_SKIP_ROOT_CHECK:-0}" != "1" ] && [ "$(id -u)" != "0" ]; then
        fail "must run as root on the main VPS"
        return 1
    fi
    command -v docker >/dev/null 2>&1 || { fail "docker not found"; return 1; }
    command -v "$CRONTAB_BIN" >/dev/null 2>&1 || {
        fail "no $CRONTAB_BIN binary — switch the backup cron line by hand:"
        fail "  17 3 * * * root bash $DF_DIR/backup-state.sh"
        return 1
    }
    local missing=""
    local f
    for f in $STAGED; do
        [ -f "$(staged_path "$f")" ] || missing="$missing $f"
    done
    if [ -n "$missing" ]; then
        fail "staged files missing under $DF_DIR:$missing"
        fail "run pending-deploy/apply-all/deploy-all.sh first"
        return 1
    fi
    return 0
}

run_step() { # $1=num $2=desc $3...=command
    local n="$1"; shift
    local desc="$1"; shift
    step "$n" "$desc"
    if "$@"; then
        ok "step $n done"
        return 0
    fi
    fail "step $n FAILED: $desc"
    return 1
}

# --- step 6: cron-line switch ------------------------------------------------
# Idempotent: a line already calling backup-state.sh — in root's personal
# crontab or in any /etc/cron.d file — is left alone. Any other line
# containing "backup" (case-insensitive) is the legacy explicit-list backup
# cron and gets replaced with the canonical line IN THE SAME LOCATION:
# personal-crontab entries take no user field, /etc/cron.d entries require
# the root user field (a 6-field line in a personal crontab would try to run
# "root" as a command and silently never back up). No backup line anywhere
# -> fail closed with the manual recipe (a backup command is never
# fabricated). Same /etc/cron.d coverage as repoint-backup-cron.sh (step 5):
# without it, a box whose backup cron lives in /etc/cron.d (e.g. installed
# by baseline-newbox.sh) aborts this whole run at step 6.
switch_backup_cron() {
    local tab f line tmp newtab
    local canon_crontab="17 3 * * * bash $DF_DIR/backup-state.sh"
    local canon_crond="17 3 * * * root bash $DF_DIR/backup-state.sh"
    local canon_anywhere=0 legacy_crontab=0 legacy_files=""
    local n_locs=0 replaced=0 f_has_legacy
    is_backup_line() {
        case "$1" in
            *[Bb][Aa][Cc][Kk][Uu][Pp]*) return 0 ;;
            *) return 1 ;;
        esac
    }
    # An empty/missing crontab reads as empty (never fabricate from a
    # read failure either — both land on the fail-closed path below).
    tab="$("$CRONTAB_BIN" -l 2>/dev/null)" || true
    # Phase 1: scan the personal crontab. A canonical line counts as
    # canonical, never as legacy.
    # Process substitution (not a pipe): the loop must see the flags.
    # No eval/heredoc anywhere, so cron $(...) sequences are never executed.
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            *backup-state.sh*) canon_anywhere=1 ;;
            *) is_backup_line "$line" && legacy_crontab=1 ;;
        esac
    done < <(printf '%s\n' "$tab")
    # Phase 2: scan /etc/cron.d (same coverage as repoint-backup-cron.sh).
    if [ -d "$CROND_DIR" ]; then
        for f in "$CROND_DIR"/*; do
            [ -f "$f" ] || continue
            if [ ! -r "$f" ]; then
                fail "cannot read $f — refusing to switch the backup cron blind"
                return 1
            fi
            f_has_legacy=0
            while IFS= read -r line || [ -n "$line" ]; do
                case "$line" in
                    *backup-state.sh*) canon_anywhere=1 ;;
                    *) is_backup_line "$line" && f_has_legacy=1 ;;
                esac
            done < "$f"
            [ "$f_has_legacy" = 1 ] && legacy_files="${legacy_files}${f}
"
        done
    fi
    # Phase 3: decide.
    if [ "$legacy_crontab" = 0 ] && [ -z "$legacy_files" ]; then
        if [ "$canon_anywhere" = 1 ]; then
            ok "backup cron already calls backup-state.sh — no change"
            return 0
        fi
        fail "no backup cron line found in root's crontab or $CROND_DIR — refusing to fabricate one"
        fail "add it by hand, then re-run:"
        fail "  $canon_crond"
        return 1
    fi
    [ "$canon_anywhere" = 1 ] && log "canonical backup cron already present somewhere — switching remaining legacy line(s)"
    # Phase 4: replace legacy lines in place, one location at a time.
    if [ "$legacy_crontab" = 1 ]; then
        n_locs=$((n_locs + 1))
        newtab=""
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                *backup-state.sh*)
                    newtab="${newtab}${line}
" ;;
                *)
                    if is_backup_line "$line"; then
                        log "replacing legacy backup cron line in root's crontab: $line"
                        newtab="${newtab}${canon_crontab}
"
                        replaced=$((replaced + 1))
                    else
                        newtab="${newtab}${line}
"
                    fi ;;
            esac
        done < <(printf '%s\n' "$tab")
        printf '%s' "$newtab" | "$CRONTAB_BIN" - 2>/dev/null || {
            fail "could not install updated crontab"
            return 1
        }
    fi
    while IFS= read -r f || [ -n "$f" ]; do
        [ -n "$f" ] || continue
        n_locs=$((n_locs + 1))
        tmp="$(mktemp)" || { fail "mktemp failed"; return 1; }
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                *backup-state.sh*)
                    printf '%s\n' "$line" >> "$tmp" ;;
                *)
                    if is_backup_line "$line"; then
                        log "replacing legacy backup cron line in $f: $line"
                        printf '%s\n' "$canon_crond" >> "$tmp"
                        replaced=$((replaced + 1))
                    else
                        printf '%s\n' "$line" >> "$tmp"
                    fi ;;
            esac
        done < "$f"
        # Preserve mode/owner: copy over the original instead of mv
        # (same approach as repoint-backup-cron.sh).
        if cat "$tmp" > "$f" 2>/dev/null; then
            rm -f "$tmp"
        else
            rm -f "$tmp"
            fail "cannot write $f (need root?)"
            return 1
        fi
    done < <(printf '%s' "$legacy_files")
    [ "$n_locs" -gt 1 ] && log "WARNING: legacy backup cron lines lived in $n_locs locations — all switched; remove duplicates so the backup fires once"
    # Phase 5: belt and braces — re-read and confirm the canonical line
    # landed in at least one location.
    tab="$("$CRONTAB_BIN" -l 2>/dev/null)" || true
    if printf '%s\n' "$tab" | grep -q "backup-state.sh"; then
        ok "backup cron switched ($replaced line(s) replaced)"
        return 0
    fi
    if [ -d "$CROND_DIR" ]; then
        for f in "$CROND_DIR"/*; do
            [ -f "$f" ] || continue
            if grep -q "backup-state.sh" "$f" 2>/dev/null; then
                ok "backup cron switched ($replaced line(s) replaced)"
                return 0
            fi
        done
    fi
    fail "cron switch did not take — verify by hand: $CRONTAB_BIN -l and $CROND_DIR"
    return 1
}

# --- step 7: backup run + drill ----------------------------------------------
backup_and_drill() {
    local backups archive newest
    backups="${JERUSALEM_BACKUPS:-$DF_DIR/../jerusalem-backups}"
    JERUSALEM_DATA="${JERUSALEM_DATA:-$DF_DIR}" \
    JERUSALEM_BACKUPS="$backups" \
        bash "$DF_DIR/backup-state.sh" || {
        fail "backup-state.sh run failed"
        return 1
    }
    newest=""
    for archive in "$backups"/jerusalem-state-*.tar.gz; do
        [ -f "$archive" ] || continue
        if [ -z "$newest" ] || [ "$archive" -nt "$newest" ]; then
            newest="$archive"
        fi
    done
    [ -n "$newest" ] || { fail "no archive produced in $backups"; return 1; }
    log "drilling newest archive: $(basename "$newest")"
    bash "$DF_DIR/verify-backup.sh" "$newest" || {
        fail "verify-backup.sh FAILED on $newest — do NOT trust this backup"
        return 1
    }
    ok "backup + drill passed"
    return 0
}

# --- main --------------------------------------------------------------------
main() {
    local order="preflight keepalive booted-watcher screencap optimizer repoint cron-switch backup-drill"
    local done_steps="" s
    preflight || { fail "preflight failed"; return 1; }
    ok "preflight: all 18 staged files present"
    done_steps=" preflight"

    for s in keepalive booted-watcher screencap optimizer repoint cron-switch backup-drill; do
        case "$s" in
            keepalive)
                run_step 1 "push keepalive v2 into live containers" \
                    bash "$DF_DIR/push-keepalive.sh" "$DF_DIR/roblox-keepalive.sh" || break ;;
            booted-watcher)
                run_step 2 "push booted-watcher into live containers" \
                    bash "$DF_DIR/push-booted-watcher.sh" "$DF_DIR/booted-watcher.sh" || break ;;
            screencap)
                run_step 3 "push adaptive screencap loop into live containers" \
                    env PUSH_SCREENCAP_SRC="$DF_DIR/screencap-loop.sh" bash "$DF_DIR/push-screencap.sh" || break ;;
            optimizer)
                run_step 4 "push android optimizer into live containers" \
                    bash "$DF_DIR/push-optimizer.sh" "$DF_DIR/android-optimize.sh" || break ;;
            repoint)
                run_step 5 "repoint 03:17 backup cron to $DF_DIR" \
                    env BACKUP_CRONTAB="$CRONTAB_BIN" \
                        bash "$DF_DIR/repoint-backup-cron.sh" || break ;;
            cron-switch)
                run_step 6 "switch backup cron line to backup-state.sh" \
                    switch_backup_cron || break ;;
            backup-drill)
                run_step 7 "run backup-state.sh once + drill the archive" \
                    backup_and_drill || break ;;
        esac
        done_steps="$done_steps $s"
    done

    echo "[finish-deploy] ---- summary ----"
    for s in $order; do
        case " $done_steps " in
            *" $s "*) echo "[finish-deploy]   [ok]   $s" ;;
            *)       echo "[finish-deploy]   [FAIL] $s" ;;
        esac
    done
    case " $done_steps " in
        *" backup-drill "*)
            ok "all post-deploy steps complete"
            log "NEXT (from the repo machine):"
            log "  bash pending-deploy/live-verify/verify-live.sh --ssh"
            return 0 ;;
        *)
            fail "stopped early — fix the failed step, then re-run (idempotent)"
            return 1 ;;
    esac
}

main "$@"
