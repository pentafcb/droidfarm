#!/bin/bash
# verify-backup.sh <archive.tar.gz> — integrity drill for a state backup.
#
# Answers "could I restore from this archive?" without printing any file
# contents (logs carry names only — never passwords, tokens or accounts).
# Intended use: manual restore drills and post-backup spot checks.
#
# Exit 0: archive reads, every member lists, every .json parses, the full
# core state set is present. Exit 1: problems found (listed on stdout).
# Exit 2: usage or IO error (unreadable archive, missing python3, ...).
set -u

# Must stay in sync with backup-state.sh's STATE_FILES (enforced by test).
STATE_FILES="instances.json providers.json tokens.json roblox_accounts.json roblox_login_results.json static_join_token"

say()  { echo "[verify-backup] $*"; }
warn() { echo "[verify-backup] WARNING: $*" >&2; }

arc="${1:-}"
[ -n "$arc" ] || { say "usage: verify-backup.sh <archive.tar.gz>"; exit 2; }
[ -r "$arc" ] || { say "cannot read archive: $arc"; exit 2; }
command -v python3 >/dev/null 2>&1 || { say "python3 not found — cannot validate JSON"; exit 2; }

members="$(tar -tzf "$arc" 2>/dev/null)" || { say "not a readable tar.gz: $arc"; exit 2; }
[ -n "$members" ] || { say "archive holds zero members: $arc"; exit 1; }
say "members: $(printf '%s' "$members" | wc -l)"

vdir="$(mktemp -d)" || { say "cannot create temp dir"; exit 2; }
trap 'rm -rf "$vdir"' EXIT
tar -xzf "$arc" -C "$vdir" 2>/dev/null || { say "extract failed: $arc"; exit 1; }

problems=0
while IFS= read -r m; do
    case "$m" in *.json)
        if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$vdir/$m" 2>/dev/null; then
            say "json ok: $m"
        else
            say "PROBLEM: not valid JSON: $m"; problems=$((problems + 1))
        fi ;;
    esac
done <<< "$members"

for f in $STATE_FILES; do
    if printf '%s\n' "$members" | grep -qx "$f"; then
        say "present: $f"
    else
        say "PROBLEM: core state file missing: $f"; problems=$((problems + 1))
    fi
done
if printf '%s\n' "$members" | grep -q '^batch_jobs/'; then
    say "present: batch_jobs/ journals"
else
    warn "no batch_jobs/ journals in this archive (predates fleet jobs, or none journaled)"
fi

trap - EXIT
rm -rf "$vdir"
if [ "$problems" = 0 ]; then
    say "VERDICT: archive is restorable"
    exit 0
else
    say "VERDICT: $problems problem(s) — see PROBLEM lines above"
    exit 1
fi
