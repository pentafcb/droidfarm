#!/usr/bin/env bash
# test_fetch_roblox_apks.sh — hermetic tests for fetch-roblox-apks.sh.
# Fake curl/id on PATH; DF_PIN_* overrides point at small fixture zips.
# Real pins are asserted against the real backup splits (when present).
set -u

SCRIPT="$(cd "$(dirname "$0")" && pwd)/fetch-roblox-apks.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $*"; }

BIN="$T/bin"; mkdir -p "$BIN"
export PATH="$BIN:$PATH"

# --- fixture zips -----------------------------------------------------------
python3 - "$T/base.apk" <<'EOF'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1], "w")
z.writestr("AndroidManifest.xml", b"<manifest/>")
z.writestr("classes.dex", b"dex")
z.close()
EOF
python3 - "$T/x86.apk" <<'EOF'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1], "w")
z.writestr("AndroidManifest.xml", b"<manifest/>")
z.writestr("lib/x86_64/libroblox.so", b"elf")
z.close()
EOF
python3 - "$T/no-manifest.apk" <<'EOF'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1], "w")
z.writestr("classes.dex", b"dex")
z.close()
EOF
python3 - "$T/no-x86.apk" <<'EOF'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1], "w")
z.writestr("AndroidManifest.xml", b"<manifest/>")
z.writestr("lib/arm64-v8a/libroblox.so", b"elf")
z.close()
EOF
echo "not a zip at all" > "$T/html-page"

PIN_BASE="$(sha256sum "$T/base.apk" | cut -d' ' -f1)"
PIN_X86="$(sha256sum "$T/x86.apk" | cut -d' ' -f1)"
PIN_NOMANIFEST="$(sha256sum "$T/no-manifest.apk" | cut -d' ' -f1)"
PIN_NOX86="$(sha256sum "$T/no-x86.apk" | cut -d' ' -f1)"
PIN_HTML="$(sha256sum "$T/html-page" | cut -d' ' -f1)"

# --- fake id (root) ----------------------------------------------------------
cat > "$BIN/id" <<'EOF'
#!/usr/bin/env bash
echo 0
EOF
chmod +x "$BIN/id"

# --- fake curl: serves canned payloads, logs invocations ---------------------
# CURL_MODE: ok | fail | html ; CURL_BASE_SRC / CURL_X86_SRC: fixture paths
: > "$T/curl.calls"
cat > "$BIN/curl" <<'EOF'
#!/usr/bin/env bash
echo "curl $*" >> "$CURL_CALL_LOG"
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -*) shift;;
    *)  url="$1"; shift;;
  esac
done
case "$CURL_MODE" in
  ok)   case "$url" in
          *base*) cp "$CURL_BASE_SRC" "$out";;
          *)      cp "$CURL_X86_SRC" "$out";;
        esac;;
  fail) exit 22;;
  html) cp "$CURL_HTML_SRC" "$out";;
esac
EOF
chmod +x "$BIN/curl"
export CURL_CALL_LOG="$T/curl.calls"

run_fetch() { # $1=dest ; rest passed through as env already exported
  DF_PIN_BASE_SHA="$PIN_BASE" DF_PIN_X86_SHA="$PIN_X86" \
    bash "$SCRIPT" "https://example.invalid/base.apk?token=SECRET-TOKEN-XYZ" \
                   "https://example.invalid/x86.apk?token=SECRET-TOKEN-XYZ" "$1"
}

# --- 1: usage ----------------------------------------------------------------
out="$(bash "$SCRIPT" 2>&1)"; rc=$?
[ $rc -ne 0 ] && echo "$out" | grep -q "usage" && pass || fail "usage/0-args (rc=$rc)"
out="$(bash "$SCRIPT" only-one 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass || fail "usage/1-arg (rc=$rc)"

# --- 2: non-root refused ------------------------------------------------------
cat > "$BIN/id" <<'EOF'
#!/usr/bin/env bash
echo 1000
EOF
out="$(DF_PIN_BASE_SHA="$PIN_BASE" DF_PIN_X86_SHA="$PIN_X86" bash "$SCRIPT" u1 u2 "$T/d" 2>&1)"; rc=$?
[ $rc -ne 0 ] && echo "$out" | grep -q "run as root" && pass || fail "non-root refused (rc=$rc)"
cat > "$BIN/id" <<'EOF'
#!/usr/bin/env bash
echo 0
EOF

# --- 3: curl missing -----------------------------------------------------------
# Shim dir with everything the script needs EXCEPT curl -> deterministic.
SHIM="$T/shimbin"; mkdir -p "$SHIM"
for t in python3 sha256sum mkdir stat mv rm cut; do
  p="$(command -v "$t")" && ln -sf "$p" "$SHIM/$t"
done
cp "$BIN/id" "$SHIM/id"
BASH_BIN="$(command -v bash)"
sed -i "1s|.*|#!$BASH_BIN|" "$SHIM/id"   # env-bash shebang can't resolve under the stripped PATH
out="$(env PATH="$SHIM" DF_PIN_BASE_SHA="$PIN_BASE" DF_PIN_X86_SHA="$PIN_X86" \
  "$BASH_BIN" "$SCRIPT" u1 u2 "$T/d" 2>&1)"; rc=$?
[ $rc -eq 1 ] && echo "$out" | grep -q "curl not found" && pass || fail "curl-missing refused (rc=$rc)"

# --- 4: happy path --------------------------------------------------------------
export CURL_MODE=ok CURL_BASE_SRC="$T/base.apk" CURL_X86_SRC="$T/x86.apk"
D="$T/dest1"
out="$(run_fetch "$D" 2>&1)"; rc=$?
[ $rc -eq 0 ] && pass || fail "happy path exit 0 (rc=$rc)"
cmp -s "$T/base.apk" "$D/roblox-base.apk" && pass || fail "base staged byte-identical"
cmp -s "$T/x86.apk" "$D/roblox-x86_64.apk" && pass || fail "x86 staged byte-identical"
[ -z "$(ls -A "$D"/.fetch-roblox-*.tmp.* 2>/dev/null)" ] && pass || fail "no tmp leftovers"
echo "$out" | grep -q "roblox-base.apk size=" && pass || fail "logs name/size"
echo "$out" | grep -q "SECRET-TOKEN-XYZ" && fail "URL token leaked in output" || pass
calls_before="$(wc -l < "$T/curl.calls")"

# --- 5: idempotent skip ----------------------------------------------------------
out="$(run_fetch "$D" 2>&1)"; rc=$?
[ $rc -eq 0 ] && pass || fail "idempotent exit 0 (rc=$rc)"
[ "$(wc -l < "$T/curl.calls")" = "$calls_before" ] && pass || fail "idempotent: curl not called"
echo "$out" | grep -q "already pinned" && pass || fail "idempotent log line"

# --- 6: corrupt staged file -> re-download ---------------------------------------
echo "garbage" > "$D/roblox-base.apk"
out="$(run_fetch "$D" 2>&1)"; rc=$?
[ $rc -eq 0 ] && cmp -s "$T/base.apk" "$D/roblox-base.apk" && pass || fail "corrupt staged re-downloaded (rc=$rc)"

# --- 7: curl failure -> fail closed ----------------------------------------------
export CURL_MODE=fail
D2="$T/dest2"
out="$(run_fetch "$D2" 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass || fail "curl-fail exit non-zero (rc=$rc)"
[ ! -e "$D2/roblox-base.apk" ] && [ ! -e "$D2/roblox-x86_64.apk" ] && pass || fail "curl-fail: nothing staged"
[ -z "$(ls -A "$D2"/.fetch-roblox-*.tmp.* 2>/dev/null)" ] && pass || fail "curl-fail: tmp cleaned"
echo "$out" | grep -q "download failed" && pass || fail "curl-fail message"

# --- 8: HTML error page (expired link) -> pin mismatch, fail closed ---------------
export CURL_MODE=html CURL_HTML_SRC="$T/html-page"
D3="$T/dest3"
out="$(run_fetch "$D3" 2>&1)"; rc=$?
[ $rc -ne 0 ] && pass || fail "html-page exit non-zero (rc=$rc)"
echo "$out" | grep -qi "expired" && pass || fail "html-page names expired-link cause"
[ ! -e "$D3/roblox-base.apk" ] && pass || fail "html-page: bad bytes never staged"
[ -z "$(ls -A "$D3"/.fetch-roblox-*.tmp.* 2>/dev/null)" ] && pass || fail "html-page: tmp cleaned"

# --- 9: zip sanity ---------------------------------------------------------------
export CURL_MODE=ok
D4="$T/dest4"
out="$(DF_PIN_BASE_SHA="$PIN_NOMANIFEST" DF_PIN_X86_SHA="$PIN_X86" \
  CURL_BASE_SRC="$T/no-manifest.apk" CURL_X86_SRC="$T/x86.apk" \
  bash "$SCRIPT" "https://example.invalid/b" "https://example.invalid/x" "$D4" 2>&1)"; rc=$?
[ $rc -ne 0 ] && [ ! -e "$D4/roblox-base.apk" ] && pass || fail "base without manifest rejected (rc=$rc)"

D5="$T/dest5"
out="$(DF_PIN_BASE_SHA="$PIN_BASE" DF_PIN_X86_SHA="$PIN_NOX86" \
  CURL_BASE_SRC="$T/base.apk" CURL_X86_SRC="$T/no-x86.apk" \
  bash "$SCRIPT" "https://example.invalid/b" "https://example.invalid/x" "$D5" 2>&1)"; rc=$?
[ $rc -ne 0 ] && [ ! -e "$D5/roblox-x86_64.apk" ] && pass || fail "split without x86_64 lib rejected (rc=$rc)"

# --- 10: dest auto-created --------------------------------------------------------
D6="$T/nonexistent/nested/dir"
out="$(run_fetch "$D6" 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ -f "$D6/roblox-base.apk" ] && pass || fail "dest auto-created (rc=$rc)"

# --- 11: default pins match the real verified backup splits -----------------------
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
if [ -f "$REPO/roblox-apks/roblox-base.apk" ] && [ -f "$REPO/roblox-apks/roblox-x86_64.apk" ]; then
  real_base="$(sha256sum "$REPO/roblox-apks/roblox-base.apk" | cut -d' ' -f1)"
  real_x86="$(sha256sum "$REPO/roblox-apks/roblox-x86_64.apk" | cut -d' ' -f1)"
  grep -q "PIN_BASE_SHA_DEFAULT=\"$real_base\"" "$SCRIPT" && pass || fail "default base pin == verified backup bytes"
  grep -q "PIN_X86_SHA_DEFAULT=\"$real_x86\"" "$SCRIPT" && pass || fail "default x86 pin == verified backup bytes"
else
  echo "SKIP: roblox-apks/ fixtures absent (pin-tie check)"
fi

# --- 12: curl hardening flags present ----------------------------------------------
grep -q -- "--fail" "$SCRIPT" && grep -q -- "--location" "$SCRIPT" \
  && grep -q -- "--retry" "$SCRIPT" && pass || fail "curl --fail/--location/--retry present"

# --- 13: bash -n -------------------------------------------------------------------
bash -n "$SCRIPT" && pass || fail "bash -n"

echo "$PASS passed, $FAIL failed"
[ "$FAIL" = "0" ]
