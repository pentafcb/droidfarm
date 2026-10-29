#!/bin/bash
# baseline-newbox.sh — establish the host-level operational baseline on a fresh
# Jerusalem 1.0 install (the new VPS 64.177.49.68).
#
# The 2026-10-12 install put the manager, service, Docker, ffmpeg and the
# hourly log trimmer on the new box — but NOT the minute-cron watchdog, the
# daily state backup, or Docker log rotation. Without them the new box has no
# auto-recovery and no backups when the fleet lands on it.
#
# Runs ON the VPS as root. Idempotent: re-running is always safe (installs
# only on sha256 difference, cron files are exact-content, daemon.json is
# merged semantically, the backup drill re-runs harmlessly).
#
# Usage: baseline-newbox.sh <watchdog_src> <backup_state_src> <verify_backup_src>
#   The three sources are staged files (scp them to the box first); each is
#   bash -n gated before install. Existing targets are kept as <.prev> when
#   replaced. Nothing here touches instance data, volumes or containers.
#
# Steps:
#   1. preflight: root, docker present, jerusalem.service state (warn-only)
#   1b. ensure the cron daemon is installed and active (warn-only — steps 3
#      and 7 write cron files that never fire without it)
#   2. install /usr/local/bin/df-watchdog.sh from <watchdog_src>
#   3. minute cron /etc/cron.d/jerusalem-watchdog (skipped when root's personal
#      crontab already covers df-watchdog — never clobbers a user crontab)
#   4. /etc/docker/daemon.json merged to log-driver json-file +
#      log-opts max-size 10m / max-file 3 (the old box's rotation); docker
#      reloaded only when the file actually changed (warn-only on failure)
#   5. binder device count check (warn when below NB_MIN_BINDER; the manager
#      self-heals the pool at instance-create time — this only surfaces it)
#   6. install backup-state.sh + verify-backup.sh into /root/jerusalem/
#   7. daily cron /etc/cron.d/jerusalem-backup -> backup-state.sh at 03:17
#      (same personal-crontab deference as step 3)
#   8. backup drill: run backup-state.sh once + verify-backup.sh on the
#      newest archive (proves the new cron line works end to end)
#   9. summary; exit 0 all-ok / 1 on any hard failure.
#
# Env overrides (hermetic tests):
#   NB_CRON_D      default /etc/cron.d
#   NB_BINDIR      default /usr/local/bin
#   NB_DOCKER_JSON default /etc/docker/daemon.json
#   NB_JDIR        default /root/jerusalem
#   NB_BINDER_GLOB default /dev/binderfs/binder*
#   NB_MIN_BINDER  default 60
#   NB_NO_RELOAD=1 skips `systemctl reload docker` (tests fake systemctl anyway)
# Logs carry names/counts only — never file contents, passwords or tokens.
set -u

WD_SRC="${1:-}"; BS_SRC="${2:-}"; VB_SRC="${3:-}"
CRON_D="${NB_CRON_D:-/etc/cron.d}"
BINDIR="${NB_BINDIR:-/usr/local/bin}"
DOCKER_JSON="${NB_DOCKER_JSON:-/etc/docker/daemon.json}"
JDIR="${NB_JDIR:-/root/jerusalem}"
BINDER_GLOB="${NB_BINDER_GLOB:-/dev/binderfs/binder*}"
MIN_BINDER="${NB_MIN_BINDER:-60}"

FAILC=0; WARNC=0
say()  { echo "[newbox-baseline] $*"; }
warn() { WARNC=$((WARNC+1)); echo "[newbox-baseline] WARNING: $*" >&2; }
fail() { FAILC=$((FAILC+1)); echo "[newbox-baseline] FAIL: $*" >&2; }
ok()   { echo "[newbox-baseline] [ok] $*"; }
already() { echo "[newbox-baseline] [already] $*"; }

# --- 0. args ---
[ -n "$WD_SRC" ] && [ -n "$BS_SRC" ] && [ -n "$VB_SRC" ] || {
    say "usage: baseline-newbox.sh <watchdog_src> <backup_state_src> <verify_backup_src>"
    exit 2
}
for f in "$WD_SRC" "$BS_SRC" "$VB_SRC"; do
    [ -f "$f" ] || { fail "source missing: $f"; }
done
[ "$FAILC" = 0 ] || exit 1

# --- 1. preflight ---
[ "$(id -u 2>/dev/null)" = "0" ] || { fail "must run as root"; exit 1; }
command -v docker >/dev/null 2>&1 || { fail "docker not found on PATH"; exit 1; }
command -v python3 >/dev/null 2>&1 || { fail "python3 not found (needed for daemon.json merge + backup JSON validation)"; exit 1; }
command -v bash >/dev/null 2>&1 || { fail "bash not found"; exit 1; }
if command -v systemctl >/dev/null 2>&1; then
    if [ "$(systemctl is-active jerusalem 2>/dev/null)" = "active" ]; then
        ok "jerusalem.service active"
    else
        warn "jerusalem.service not active (is-active: $(systemctl is-active jerusalem 2>/dev/null || echo unknown))"
    fi
else
    warn "systemctl not found — skipping service check"
fi
mkdir -p "$CRON_D" "$BINDIR" "$JDIR" 2>/dev/null || { fail "cannot create $CRON_D / $BINDIR / $JDIR"; exit 1; }

# --- cron-ensure-fn-begin ---
# ensure_cron_daemon: the minute-cron watchdog (step 3) and the daily backup
# cron (step 7) never fire on a box without a running cron daemon (minimal
# images don't always ship one). Warn-only by design: the cron files are
# still written, and verify-live.sh --ssh flags a dead daemon — so a broken
# apt or a missing systemd can never block the rest of the baseline.
ensure_cron_daemon() {
    if command -v systemctl >/dev/null 2>&1 && [ "$(systemctl is-active cron 2>/dev/null)" = "active" ]; then
        ok "cron daemon active"
        return 0
    fi
    # Install the package only when the crontab binary is missing entirely —
    # when it exists, the package is present and only the daemon needs
    # (re)starting. (Also keeps hermetic test envs with a fake crontab on
    # PATH, but no fake apt-get, from ever reaching a real apt-get.)
    if ! command -v crontab >/dev/null 2>&1; then
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update -qq >/dev/null 2>&1 || true
            if apt-get install -y -qq cron >/dev/null 2>&1; then
                ok "installed the cron package"
            else
                warn "could not install the cron package"
            fi
        else
            warn "apt-get not found — cannot install the cron package"
        fi
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now cron >/dev/null 2>&1 || true
        if [ "$(systemctl is-active cron 2>/dev/null)" = "active" ]; then
            ok "cron daemon enabled and active"
        else
            warn "cron daemon not active — the minute-cron watchdog and the daily backup cron will not fire until cron is running"
        fi
    else
        warn "systemctl not found — cannot start the cron daemon"
    fi
}
# --- cron-ensure-fn-end ---

# --- 1b. cron daemon (warn-only; steps 3 and 7 write cron files regardless) ---
ensure_cron_daemon

# install_file <src> <dst>: bash -n gate, install only on sha256 difference,
# keep a single .prev of a replaced target. Prints ok/already.
install_file() {
    local src="$1" dst="$2" ssum dsum
    bash -n "$src" 2>/dev/null || { fail "bash -n failed: $src"; return 1; }
    ssum=$(sha256sum < "$src" 2>/dev/null | cut -d' ' -f1)
    if [ -f "$dst" ]; then
        dsum=$(sha256sum < "$dst" 2>/dev/null | cut -d' ' -f1)
        if [ "$ssum" = "$dsum" ]; then already "$dst up to date"; return 0; fi
        cp -a "$dst" "$dst.prev" 2>/dev/null || { fail "cannot back up $dst"; return 1; }
    fi
    cp -a "$src" "$dst" 2>/dev/null || { fail "cannot install $dst"; return 1; }
    chmod 755 "$dst" 2>/dev/null
    ok "installed $dst"
}

# in_personal_crontab <pattern>: true when root's personal crontab mentions it
in_personal_crontab() {
    crontab -l 2>/dev/null | grep -q "$1" 2>/dev/null
}

# ensure_cronfile <name> <line> <pattern>: exact-content cron.d file, unless
# the personal crontab already covers <pattern>.
ensure_cronfile() {
    local name="$1" line="$2" pattern="$3"
    local f="$CRON_D/$name"   # separate line: $name must be the local, not the outer scope
    if in_personal_crontab "$pattern"; then
        already "$pattern already in root's personal crontab — leaving cron.d alone"
        return 0
    fi
    if [ -f "$f" ] && [ "$(cat "$f" 2>/dev/null)" = "$line" ]; then
        already "$f"
        return 0
    fi
    printf '%s\n' "$line" > "$f" 2>/dev/null || { fail "cannot write $f"; return 1; }
    chmod 644 "$f" 2>/dev/null
    ok "wrote $f"
}

# --- 2. watchdog ---
install_file "$WD_SRC" "$BINDIR/df-watchdog.sh" || exit 1

# --- 3. watchdog minute cron ---
ensure_cronfile "jerusalem-watchdog" "* * * * * root $BINDIR/df-watchdog.sh" "df-watchdog" || exit 1

# --- 4. docker log rotation ---
# Merge semantically with python3 (present on every Jerusalem box): existing
# keys preserved, log rotation enforced. The helper prints CHANGED/UNCHANGED
# and only writes on CHANGED, so re-runs are byte-identical no-ops.
_merge_daemon_json() {
    python3 - "$DOCKER_JSON" <<'PYEOF'
import json, sys
p = sys.argv[1]
try:
    with open(p) as f:
        cur = json.load(f)
except FileNotFoundError:
    cur = {}
except Exception as e:
    print("INVALID: %s" % e)
    sys.exit(0)
if not isinstance(cur, dict):
    print("INVALID: top-level JSON is not an object")
    sys.exit(0)
want = dict(cur)
want["log-driver"] = "json-file"
lo = want.get("log-opts")
if not isinstance(lo, dict):
    lo = {}
lo = dict(lo)
lo["max-size"] = "10m"
lo["max-file"] = "3"
want["log-opts"] = lo
if want == cur:
    print("UNCHANGED")
else:
    with open(p, "w") as f:
        json.dump(want, f, indent=2)
        f.write("\n")
    print("CHANGED")
PYEOF
}
_dj_merged=0
_errf="/tmp/nb-baseline-err.$$"
if [ ! -e "$DOCKER_JSON" ]; then
    mkdir -p "$(dirname "$DOCKER_JSON")" 2>/dev/null
    _dj_out=$(_merge_daemon_json 2>"$_errf") || { fail "daemon.json merge crashed"; cat "$_errf" >&2 2>/dev/null; }
    case "${_dj_out:-}" in
        CHANGED) ok "created $DOCKER_JSON with log rotation (10m x 3)"; _dj_merged=1;;
        *) fail "unexpected daemon.json merge result: ${_dj_out:-empty}";;
    esac
else
    _dj_out=$(_merge_daemon_json 2>"$_errf") || { fail "daemon.json merge crashed"; cat "$_errf" >&2 2>/dev/null; }
    case "${_dj_out:-}" in
        UNCHANGED) already "$DOCKER_JSON already has log rotation";;
        CHANGED) ok "merged log rotation into $DOCKER_JSON (other keys preserved)"; _dj_merged=1;;
        INVALID*) fail "$DOCKER_JSON ${_dj_out#INVALID: } — refusing to clobber; fix by hand";;
        *) fail "unexpected daemon.json merge result: ${_dj_out:-empty}";;
    esac
fi
rm -f "$_errf" 2>/dev/null
if [ "$_dj_merged" = 1 ] && [ "${NB_NO_RELOAD:-0}" != "1" ]; then
    if systemctl reload docker 2>"$_errf"; then
        ok "docker reloaded (containers unaffected)"
    else
        warn "systemctl reload docker failed — rotation applies on next docker restart ($(head -1 "$_errf" 2>/dev/null))"
    fi
    rm -f "$_errf" 2>/dev/null
fi
[ "$FAILC" = 0 ] || exit 1

# --- 5. binder device count (surface only; manager self-heals at create time) ---
_nb_binders=$(ls $BINDER_GLOB 2>/dev/null | wc -l)
if [ "$_nb_binders" -ge "$MIN_BINDER" ]; then
    ok "binder devices: $_nb_binders (>= $MIN_BINDER)"
else
    warn "only $_nb_binders binder devices (< $MIN_BINDER) — manager self-heals the pool at instance-create time"
fi

# --- 6. backup scripts ---
install_file "$BS_SRC" "$JDIR/backup-state.sh" || exit 1
install_file "$VB_SRC" "$JDIR/verify-backup.sh" || exit 1

# --- 7. daily backup cron (canonical form: calls backup-state.sh) ---
ensure_cronfile "jerusalem-backup" "17 3 * * * root bash $JDIR/backup-state.sh" "backup-state" || exit 1

# --- 8. backup drill: one real run + verify newest archive ---
if JERUSALEM_DATA="$JDIR" "$JDIR/backup-state.sh" >/tmp/nb-backup-out 2>&1; then
    _bkdir="${JERUSALEM_BACKUPS:-/root/jerusalem-backups}"
    _newest=$(ls -t "$_bkdir"/jerusalem-state-*.tar.gz 2>/dev/null | head -1)
    if [ -n "$_newest" ] && "$JDIR/verify-backup.sh" "$_newest" >/tmp/nb-verify-out 2>&1; then
        ok "backup drill passed ($(basename "$_newest"))"
    else
        fail "backup drill failed — see /tmp/nb-verify-out"
        cat /tmp/nb-verify-out >&2 2>/dev/null
    fi
else
    fail "backup-state.sh run failed — see /tmp/nb-backup-out"
    cat /tmp/nb-backup-out >&2 2>/dev/null
fi
[ "$FAILC" = 0 ] || exit 1

# --- 9. service re-check for the summary ---
if command -v systemctl >/dev/null 2>&1; then
    [ "$(systemctl is-active jerusalem 2>/dev/null)" = "active" ] \
        && ok "jerusalem.service active" \
        || warn "jerusalem.service not active at end of run"
fi

say "done: $WARNC warning(s), $FAILC failure(s)"
[ "$FAILC" = 0 ]
