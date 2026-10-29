#!/bin/bash
# import-state.sh — import Jerusalem 1.0 manager state into the NEW box.
#
# The fleet's portable data (Roblox account mapping, login history,
# fleet-job journals) lives on the OLD box's state dir. When the fleet is
# recreated on the new box (64.177.49.68), importing that data keeps the
# account mapping without anyone retyping the credential lines.
#
# Consumes an archive produced by backup-state.sh on the SOURCE box:
#   1. on the OLD box:  bash /root/jerusalem/backup-state.sh
#      (or take the newest /root/jerusalem-backups/jerusalem-state-*.tar.gz)
#   2. scp it to the NEW box
#   3. on the NEW box:  bash import-state.sh <archive.tar.gz>
#
# Default import set (portable, topology-free):
#   roblox_accounts.json, roblox_login_results.json, batch_jobs/*.json
# The destination's topology + identity are PRESERVED by default:
#   instances.json, providers.json, tokens.json, static_join_token
# are left exactly as they are.
# --full also imports instances.json + providers.json — ONLY for a
# full-box swap. Those entries point at the OLD host's containers, so do
# NOT use --full when the new box will create its own fleet.
#
# tokens.json / static_join_token are NEVER imported (join-token
# continuity for enrolled wing nodes is Penta's decision; the new box
# keeps its own identity).
#
# Steps: root check -> archive whitelist check (fail closed on hostile
# members) -> snapshot current dest state (timestamped, 700, reversible)
# -> stop jerusalem (warn-only) -> extract members to temp, JSON-validate
# -> install atomically (600 files, 700 dirs; journals merged, never
# deleted) -> start service (warn-only) -> verify is-active + dashboard
# 200 (warn-only). Logs carry names/counts/sizes only — never contents.
#
# Env overrides (also used by the unit tests):
#   JERUSALEM_DATA    dest state dir (default /root/jerusalem)
#   SM_BACKUP_DIR     snapshot dir (default $JERUSALEM_DATA/.pre-import)
#   SM_SERVICE        service name (default jerusalem)
#   SM_NO_SVC         set to 1 to skip systemctl stop/start/is-active
#   SM_NO_DASHBOARD   set to 1 to skip the dashboard 200 probe
#   SM_PORT           dashboard port for the probe (default 8080)
set -u

DATA="${JERUSALEM_DATA:-/root/jerusalem}"
BKDIR="${SM_BACKUP_DIR:-$DATA/.pre-import}"
SVC="${SM_SERVICE:-jerusalem}"
NO_SVC="${SM_NO_SVC:-0}"
NO_DASH="${SM_NO_DASHBOARD:-0}"
PORT="${SM_PORT:-8080}"

# Canonical state set, mirrored from backup-state.sh STATE_FILES.
STATE_FILES="instances.json providers.json tokens.json roblox_accounts.json roblox_login_results.json static_join_token"
# What the default (non --full) import takes.
DEFAULT_FILES="roblox_accounts.json roblox_login_results.json"

say()  { echo "[import-state] $*"; }
warn() { echo "[import-state] WARNING: $*" >&2; }
fail() { echo "[import-state] FAIL: $*" >&2; return 1; }

usage() {
    echo "usage: $0 <archive.tar.gz> [--full]" >&2
    echo "  imports portable state (roblox_accounts.json, roblox_login_results.json," >&2
    echo "  batch_jobs/) from a backup-state.sh archive into \$JERUSALEM_DATA" >&2
    echo "  --full also imports instances.json + providers.json (full-box swap only)" >&2
    echo "  never imports tokens.json / static_join_token (identity preserved)" >&2
}

# --- args ---
FULL=0
ARCH=""
for a in "$@"; do
    case "$a" in
        --full) FULL=1 ;;
        -h|--help) usage; exit 2 ;;
        -*) fail "unknown flag: $a"; usage; exit 2 ;;
        *) [ -z "$ARCH" ] && ARCH="$a" || { usage; exit 2; } ;;
    esac
done
[ -n "$ARCH" ] || { usage; exit 2; }

# --- preflight: root ---
[ "$(id -u 2>/dev/null || echo 1)" = "0" ] || { fail "must run as root"; exit 1; }
[ -r "$ARCH" ] || { fail "archive not readable: $ARCH"; exit 1; }
[ -d "$DATA" ] || { fail "state dir missing: $DATA"; exit 1; }

# --- preflight: archive whitelist (fail closed on hostile members) ---
members="$(tar -tzf "$ARCH" 2>/dev/null)" || { fail "archive unreadable (not a tar.gz?)"; exit 1; }
[ -n "$members" ] || { fail "archive holds zero members"; exit 1; }
hex16='^[0-9a-f]\{16\}\.json$'
while IFS= read -r m; do
    m="${m#./}"            # tolerate ./-prefixed members
    m="${m%/}"             # tolerate dir entries
    case "$m" in
        "") continue ;;
        batch_jobs) continue ;;
        /*|*../*|*..) fail "hostile archive member rejected: $m"; exit 1 ;;
    esac
    ok=0
    for f in $STATE_FILES; do
        [ "$m" = "$f" ] && ok=1
    done
    case "$m" in
        batch_jobs/*)
            j="${m#batch_jobs/}"
            if [ "${j#*/}" = "$j" ] && printf '%s' "$j" | grep -q "$hex16"; then
                ok=1
            fi
            ;;
    esac
    [ "$ok" = 1 ] || { fail "unexpected archive member rejected: $m"; exit 1; }
done <<< "$members"
say "archive whitelist ok: $(printf '%s\n' "$members" | wc -l) members"

# --- pick the import set (present in archive AND in our import list) ---
if [ "$FULL" = 1 ]; then
    WANT="roblox_accounts.json roblox_login_results.json instances.json providers.json"
else
    WANT="$DEFAULT_FILES"
fi
import=()
missing=()
for f in $WANT; do
    if printf '%s\n' "$members" | grep -qxE "(\./)?$f"; then
        import+=("$f")
    else
        missing+=("$f")
        warn "state file not in archive, skipped: $f"
    fi
done
journals="$(printf '%s\n' "$members" | grep -E '^(\./)?batch_jobs/[0-9a-f]{16}\.json$' || true)"
[ -z "$journals" ] && warn "no batch_jobs journals in archive, skipped"
[ "${#import[@]}" = 0 ] && [ -z "$journals" ] && \
    { fail "nothing importable in this archive"; exit 1; }

# --- snapshot current dest state (reversible) ---
snap="$BKDIR/pre-import-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$snap" || { fail "cannot create snapshot dir $snap"; exit 1; }
chmod 700 "$snap"
snapped=0
for f in $STATE_FILES; do
    if [ -f "$DATA/$f" ]; then
        cp -p "$DATA/$f" "$snap/$f" && chmod 600 "$snap/$f" && snapped=$((snapped + 1))
    fi
done
if [ -d "$DATA/batch_jobs" ]; then
    mkdir -p "$snap/batch_jobs" && chmod 700 "$snap/batch_jobs"
    for j in "$DATA"/batch_jobs/*.json; do
        [ -e "$j" ] || continue
        cp -p "$j" "$snap/batch_jobs/" && chmod 600 "$snap/batch_jobs/$(basename "$j")" \
            && snapped=$((snapped + 1))
    done
fi
say "snapshot ok: $snap ($snapped files; restore = copy back into $DATA)"

# --- stop the manager (warn-only; a down service is fine too) ---
if [ "$NO_SVC" != "1" ]; then
    systemctl stop "$SVC" 2>/dev/null \
        || warn "systemctl stop $SVC failed or service was already down — continuing"
fi

# --- extract + validate + install ---
vdir="$(mktemp -d)" || { fail "cannot create temp dir"; exit 1; }
trap 'rm -rf "$vdir"; rm -f "$DATA"/.*.import' EXIT
nfiles=0; njournals=0
for f in "${import[@]}"; do
    tar -xzf "$ARCH" -C "$vdir" "$f" 2>/dev/null \
        || tar -xzf "$ARCH" -C "$vdir" "./$f" 2>/dev/null \
        || { fail "failed to extract member: $f"; exit 1; }
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$vdir/$f" 2>/dev/null \
            || { fail "member is not valid JSON, import aborted (dest untouched): $f"; exit 1; }
    else
        warn "python3 missing — skipping JSON validation for $f"
    fi
    tmpf="$DATA/.$f.import"
    cp -p "$vdir/$f" "$tmpf" || { fail "cannot stage $f"; exit 1; }
    chmod 600 "$tmpf"
    mv -f "$tmpf" "$DATA/$f" || { fail "cannot install $f"; exit 1; }
    nfiles=$((nfiles + 1))
done
if [ -n "$journals" ]; then
    mkdir -p "$DATA/batch_jobs" || { fail "cannot create $DATA/batch_jobs"; exit 1; }
    chmod 700 "$DATA/batch_jobs"
    while IFS= read -r j; do
        j="${j#./}"
        tar -xzf "$ARCH" -C "$vdir" "$j" 2>/dev/null \
            || tar -xzf "$ARCH" -C "$vdir" "./$j" 2>/dev/null \
            || { fail "failed to extract journal: $j"; exit 1; }
        if command -v python3 >/dev/null 2>&1; then
            python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$vdir/$j" 2>/dev/null \
                || { fail "journal is not valid JSON, import aborted: $j"; exit 1; }
        fi
        cp -p "$vdir/$j" "$DATA/$j" || { fail "cannot install journal: $j"; exit 1; }
        chmod 600 "$DATA/$j"
        njournals=$((njournals + 1))
    done <<< "$journals"
fi
rm -rf "$vdir"
trap - EXIT

# --- start the manager + verify (warn-only) ---
if [ "$NO_SVC" != "1" ]; then
    systemctl start "$SVC" 2>/dev/null \
        || warn "systemctl start $SVC failed — start it by hand"
    if [ "$NO_DASH" != "1" ] && command -v curl >/dev/null 2>&1; then
        sleep 2
        code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://127.0.0.1:${PORT}/" 2>/dev/null || echo 000)"
        [ "$code" = "200" ] || warn "dashboard probe returned $code (expected 200)"
    fi
fi

say "ok: imported $nfiles state file(s) + $njournals journal(s);"
say "    preserved: tokens.json, static_join_token$([ "$FULL" = 1 ] || echo ", instances.json, providers.json")"
exit 0
