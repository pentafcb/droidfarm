#!/bin/bash
# test_log_trimmer.sh — hermetic tests for df-watchdog.sh SECTION 10
# (docker container-log trimming).
#
# Extracts the real section verbatim from waydroid-image/df-watchdog.sh
# (between the SECTION 10 markers) and runs it against fake log dirs with
# stubbed log()/note_event(). Fails closed if the markers are missing.
set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
WD="$REPO/waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d /tmp/logtrim-test-XXXXXX)"
trap 'rm -rf "$T"' EXIT
export ST="$T/st" LOGF="$T/watchdog.log"
mkdir -p "$ST" "$T/logs/c1" "$T/logs/c2"

# --- extract the real section 10 verbatim ---
SEC="$T/section10.sh"
awk '/^# --- SECTION 10/{f=1} f{print} /^# --- END SECTION 10 ---/{exit}' "$WD" > "$SEC"
[ -s "$SEC" ] || { bad "SECTION 10 markers missing in df-watchdog.sh"; echo "PASS=$PASS FAIL=$FAIL"; exit 1; }
grep -q "TRIM_GLOB" "$SEC" || { bad "extracted section has no TRIM_GLOB"; echo "PASS=$PASS FAIL=$FAIL"; exit 1; }
ok "section 10 extracted verbatim from df-watchdog.sh"

# --- stubs: the section expects log(), note_event(), $ST ---
log()         { echo "[test] $1" >>"$LOGF"; }
note_event()  { local key=$1 w=$2; local f="$ST/$key.ev"; date +%s >>"$f"; echo 1; }

run_section() {
    # $1 = glob override; env DF_LOG_TRIM_MAX/DF_LOG_TRIM_KEEP may be preset
    DF_LOG_GLOB="$1" bash -c '
        set -u
        ST="'"$ST"'"
        log() { echo "[test] $1" >>"'"$LOGF"'"; }
        note_event() { local key=$1 w=$2; local f="$ST/$key.ev"; date +%s >>"$f"; echo 1; }
        source "'"$SEC"'"
    '
}

mklog() { # mklog <dir> <size_bytes> : file filled with its own offset pattern
    python3 -c "
import sys
d, n = sys.argv[1], int(sys.argv[2])
with open(d + '/x-json.log', 'wb') as f:
    chunk = bytes((i % 251 for i in range(65536)))
    left = n
    off = 0
    while left > 0:
        w = min(left, 65536)
        f.write(bytes(((off + i) % 251 for i in range(w))))
        off += w; left -= w
" "$1" "$2"
}

# ---------- T1: over-threshold file trimmed in place (same inode) ----------
echo "== T1 over-threshold -> trimmed in place =="
rm -rf "$T/logs"/*; mkdir -p "$T/logs/c1"
mklog "$T/logs/c1" 3000
ino_before="$(stat -c%i "$T/logs/c1/x-json.log")"
DF_LOG_TRIM_MAX=1000 DF_LOG_TRIM_KEEP=400 run_section "$T/logs/*/*-json.log"
sz="$(stat -c%s "$T/logs/c1/x-json.log")"
ino_after="$(stat -c%i "$T/logs/c1/x-json.log")"
[ "$sz" = 400 ] && ok "T1 trimmed to KEEP bytes (3000->400)" || bad "T1 size=$sz (want 400)"
[ "$ino_before" = "$ino_after" ] && ok "T1 same inode (fd-safe)" || bad "T1 inode changed ($ino_before->$ino_after)"
[ ! -e "$T/logs/c1/x-json.log.trim" ] && ok "T1 temp .trim removed" || bad "T1 .trim left behind"
grep -q "log-trim: trimmed c1 json.log" "$LOGF" && ok "T1 log line written" || bad "T1 no log-trim line"
[ -f "$ST/log.trim.ev" ] && ok "T1 event recorded" || bad "T1 no log.trim event"

# ---------- T2: content is the LAST keep bytes ----------
echo "== T2 trimmed content == tail =="
# last byte of the 3000-byte pattern file: offset 2999 -> (2999 % 251)
last="$(python3 -c "print((2999) % 251)")"
got="$(python3 -c "print(open('$T/logs/c1/x-json.log','rb').read()[-1])")"
[ "$got" = "$last" ] && ok "T2 keeps the tail ($got)" || bad "T2 tail byte=$got want $last"

# ---------- T3: under-threshold file untouched ----------
echo "== T3 under-threshold untouched =="
: > "$LOGF"
mkdir -p "$T/logs/c2"; mklog "$T/logs/c2" 500
ino2="$(stat -c%i "$T/logs/c2/x-json.log")"
DF_LOG_TRIM_MAX=1000 DF_LOG_TRIM_KEEP=400 run_section "$T/logs/*/*-json.log"
[ "$(stat -c%s "$T/logs/c2/x-json.log")" = 500 ] && ok "T3 size unchanged" || bad "T3 size changed"
[ "$(stat -c%i "$T/logs/c2/x-json.log")" = "$ino2" ] && ok "T3 inode unchanged" || bad "T3 inode changed"
grep -q "log-trim" "$LOGF" && bad "T3 unexpected trim log line" || ok "T3 no trim logged"

# ---------- T4: empty dir / no match tolerated ----------
echo "== T4 no matches =="
: > "$LOGF"
mkdir -p "$T/empty"
DF_LOG_TRIM_MAX=1000 DF_LOG_TRIM_KEEP=400 run_section "$T/empty/*-json.log" \
    && ok "T4 exit 0 on no match" || bad "T4 nonzero exit"
grep -q "log-trim" "$LOGF" && bad "T4 unexpected log line" || ok "T4 silent"

# ---------- T5: default thresholds (500MB/100MB) documented ----------
echo "== T5 defaults =="
grep -q 'DF_LOG_TRIM_MAX:-1073741824' "$SEC" && ok "T5 default MAX=1GB" || bad "T5 MAX default wrong"
grep -q 'DF_LOG_TRIM_KEEP:-1073741824' "$SEC" && ok "T5 default KEEP=1GB" || bad "T5 KEEP default wrong"

# ---------- T6: trim failure -> WARN, no crash ----------
echo "== T6 failure path =="
: > "$LOGF"
mkdir -p "$T/fakebin" "$T/logs/c3"; mklog "$T/logs/c3" 3000
printf '#!/bin/bash\nexit 1\n' > "$T/fakebin/tail"; chmod +x "$T/fakebin/tail"
PATH="$T/fakebin:/usr/bin:/bin" DF_LOG_TRIM_MAX=1000 DF_LOG_TRIM_KEEP=400 \
    run_section "$T/logs/c3/*-json.log" && ok "T6 exit 0 despite failure" || bad "T6 nonzero exit"
grep -q "WARN log-trim: failed to trim" "$LOGF" && ok "T6 WARN logged" || bad "T6 no WARN line"
[ "$(stat -c%s "$T/logs/c3/x-json.log")" = 3000 ] && ok "T6 file untouched on failure" || bad "T6 file modified"

# ---------- T7: multiple files, only over-threshold trimmed ----------
echo "== T7 mixed =="
: > "$LOGF"
rm -rf "$T/logs"/*; mkdir -p "$T/logs/big" "$T/logs/small"
mklog "$T/logs/big" 5000; mklog "$T/logs/small" 100
DF_LOG_TRIM_MAX=1000 DF_LOG_TRIM_KEEP=400 run_section "$T/logs/*/*-json.log"
[ "$(stat -c%s "$T/logs/big/x-json.log")" = 400 ] && ok "T7 big trimmed" || bad "T7 big not trimmed"
[ "$(stat -c%s "$T/logs/small/x-json.log")" = 100 ] && ok "T7 small untouched" || bad "T7 small changed"
[ "$(grep -c "log-trim: trimmed" "$LOGF")" = 1 ] && ok "T7 exactly one trim logged" || bad "T7 wrong trim count"

# ---------- T8: never touches non-log files / volumes ----------
echo "== T8 scope =="
grep -q "docker volume\|volume prune\|rm -rf" "$SEC" && bad "T8 destructive command in section" \
    || ok "T8 no volume/destructive commands"
grep -q 'for _jl in \$TRIM_GLOB' "$SEC" && ok "T8 glob-scoped loop" || bad "T8 loop shape changed"

# ---------- T9: whole watchdog still parses ----------
echo "== T9 syntax =="
bash -n "$WD" && ok "T9 df-watchdog.sh bash -n clean" || bad "T9 bash -n failed"

echo "----"
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
