#!/bin/bash
# test_backup_visibility.sh — tests for the backup-state.sh status marker
# (pending-deploy/backup-visibility).
#
# GAP: the 03:17 daily state backup ran from cron with zero dashboard
# visibility — a failed or never-firing backup was invisible until a
# restore was needed. backup-state.sh now writes $DATA/last-backup.json
# on EVERY run (success and failure): atomic tmp+rename, 600 perms,
# names/counts/sizes only, never file contents.
#
# Hermetic: fake DATA/BKDIR/LOGF via env overrides.
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $1"; }
HERE="$(dirname "$0")"
SCRIPT="$HERE/../backup-state/backup-state.sh"

run_backup() { # $1=data $2=bk $3=log ; rest passed through as env
    JERUSALEM_DATA="$1" JERUSALEM_BACKUPS="$2" JERUSALEM_BACKUP_LOG="$3" \
        bash "$SCRIPT" >/tmp/bv_out.txt 2>&1
    echo $?
}

echo "backup-visibility (shell) tests"

# --- T1: happy path writes a valid ok:true marker ---
T="$(mktemp -d)"; mkdir -p "$T/d" "$T/bk"
echo '{"a":1}' > "$T/d/instances.json"
rc="$(run_backup "$T/d" "$T/bk" "$T/log")"
m="$T/d/last-backup.json"
[ "$rc" = 0 ] && [ -f "$m" ] && ok "T1 marker written on success" || bad "T1 marker written on success (rc=$rc)"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ok"] is True, d' "$m" 2>/dev/null \
    && ok "T1 marker is valid JSON with ok:true" || bad "T1 marker valid JSON ok:true"
[ "$(stat -c%a "$m" 2>/dev/null)" = 600 ] && ok "T1 marker perms 600" || bad "T1 marker perms 600"
arc="$(ls "$T/bk"/jerusalem-state-*.tar.gz | head -1)"
[ "$(python3 -c "import json;print(json.load(open('$m'))['archive'])")" = "$(basename "$arc")" ] \
    && ok "T1 archive name matches the tar" || bad "T1 archive name matches the tar"
b="$(python3 -c "import json;print(json.load(open('$m'))['bytes'])")"
[ "$b" -gt 0 ] 2>/dev/null && ok "T1 bytes>0" || bad "T1 bytes>0"
[ "$(python3 -c "import json;print(json.load(open('$m'))['files'])")" -ge 1 ] 2>/dev/null \
    && ok "T1 files>=1" || bad "T1 files>=1"
[ "$(tr -d -c '\n' < "$m" | wc -c)" = 0 ] && ok "T1 marker single line" || bad "T1 marker single line"
now="$(date -u +%s)"; epoch="$(python3 -c "import json;print(json.load(open('$m'))['ts_epoch'])")"
[ $((now - epoch)) -lt 120 ] 2>/dev/null && ok "T1 ts_epoch recent" || bad "T1 ts_epoch recent"
[ -z "$(ls "$T/d"/.tmp-last-backup.json 2>/dev/null)" ] && ok "T1 no tmp marker left" || bad "T1 no tmp marker left"

# --- T2: failure path writes ok:false marker ---
T2="$(mktemp -d)"; mkdir -p "$T2/d" "$T2/bk"   # DATA has no state files
rc="$(run_backup "$T2/d" "$T2/bk" "$T2/log")"
m2="$T2/d/last-backup.json"
[ "$rc" != 0 ] && [ -f "$m2" ] && ok "T2 marker written on failure" || bad "T2 marker written on failure (rc=$rc)"
python3 -c 'import json; d=json.load(open("'"$m2"'")); assert d["ok"] is False and d["error"], d' 2>/dev/null \
    && ok "T2 marker ok:false with error tag" || bad "T2 marker ok:false with error tag"
[ -z "$(ls "$T2/bk"/jerusalem-state-*.tar.gz 2>/dev/null)" ] && ok "T2 no archive on failure" || bad "T2 no archive on failure"
[ -z "$(ls "$T2/d"/.tmp-last-backup.json 2>/dev/null)" ] && ok "T2 no tmp marker left" || bad "T2 no tmp marker left"

# --- T3: failure then success flips the marker ---
echo '{"a":1}' > "$T2/d/instances.json"
rc="$(run_backup "$T2/d" "$T2/bk" "$T2/log")"
python3 -c 'import json; d=json.load(open("'"$m2"'")); assert d["ok"] is True, d' 2>/dev/null \
    && [ "$rc" = 0 ] && ok "T3 marker flips to ok:true on re-run" || bad "T3 marker flips to ok:true on re-run"

# --- T4: marker never contains file contents ---
T4="$(mktemp -d)"; mkdir -p "$T4/d" "$T4/bk"
echo "{\"secret\":\"BV_PLANT_$(date +%s%N)\"}" > "$T4/d/instances.json"
secret="$(python3 -c "import json;print(json.load(open('$T4/d/instances.json'))['secret'])")"
run_backup "$T4/d" "$T4/bk" "$T4/log" >/dev/null
grep -q "$secret" "$T4/d/last-backup.json" 2>/dev/null \
    && bad "T4 marker leaks file contents" || ok "T4 marker carries no file contents"

# --- T5: t40 backup-state suite still green (marker must not break it) ---
out="$(bash "$HERE/../backup-state/test_backup_state.sh" 2>&1 | tail -1)"
[ "$out" = "40 passed, 0 failed" ] && ok "T5 backup-state suite still 40/40" || bad "T5 backup-state suite still 40/40 (got: $out)"

# --- T6: bash -n clean ---
bash -n "$SCRIPT" && ok "T6 backup-state.sh bash -n clean" || bad "T6 backup-state.sh bash -n clean"

rm -rf "$T" "$T2" "$T4" /tmp/bv_out.txt
echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
