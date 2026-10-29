#!/bin/bash
# test_wing_installer_agent_check.sh — hermetic tests for the
# agent-download-check block in wing/install-wing.sh.
# A bad join token returns HTTP 403 with a JSON error body; curl without
# --fail wrote that body to agent.py and [ -s ] passed it as a "download".
# The block now adds curl --fail plus a shebang marker check that fails
# closed with a "check join token" message.
# Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$HERE/../.."
SRC="$REPO/wing/install-wing.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; mkdir -p "$BIN"
export T

# ---- fake sleep: the block sleeps 10s x 6 on a failed download ----
printf '#!/bin/bash\nexit 0\n' > "$BIN/sleep"; chmod +x "$BIN/sleep"

# ---- fake curl: behavior from FAKE_CURL_MODE ----
cat > "$BIN/curl" <<'EOF'
#!/bin/bash
out=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  [ "${args[$i]}" = "-o" ] && out="${args[$((i+1))]}"
done
[ -n "$out" ] || { echo "fake curl: no -o target" >&2; exit 1; }
case "${FAKE_CURL_MODE:-good}" in
  good)       cp "$REPO_REAL_AGENT" "$out"; exit 0 ;;   # real agent.py
  curl403)    exit 22 ;;                               # --fail on 403: no body
  body403)    printf '{"error": "invalid join token"}\n' > "$out"; exit 0 ;;  # 403 body without --fail
  html)       printf '<html><body>403 Forbidden</body></html>\n' > "$out"; exit 0 ;;  # proxy HTML page
  empty)      : > "$out"; exit 0 ;;
  badshebang) printf '#!/bin/sh\necho wrong\n' > "$out"; exit 0 ;;
esac
echo "fake curl: unknown FAKE_CURL_MODE=$FAKE_CURL_MODE" >&2; exit 1
EOF
chmod +x "$BIN/curl"
export PATH="$BIN:$PATH"
export REPO_REAL_AGENT="$REPO/wing/agent.py"

# ---- extract the product block (verbatim from the installer) ----
BEGIN_N=$(grep -c '^# BEGIN agent-download-check' "$SRC")
END_N=$(grep -c '^# END agent-download-check' "$SRC")
if [ "$BEGIN_N" = "1" ] && [ "$END_N" = "1" ]; then ok "block delimiters present exactly once"; else bad "block delimiters (BEGIN=$BEGIN_N END=$END_N)"; fi
BLOCK="$(awk '/^# BEGIN agent-download-check/{f=1;next} /^# END agent-download-check/{f=0} f' "$SRC")"
if [ -n "$BLOCK" ]; then ok "block extracted non-empty"; else bad "block extracted empty"; fi
echo "$BLOCK" > "$T/block.sh"

# ---- source contracts on the block ----
echo "$BLOCK" | grep -q -- '--fail' \
  && ok "curl uses --fail (403 body never written)" \
  || bad "curl --fail missing from the block"
echo "$BLOCK" | grep -q "head -1 .* | grep -q '^#!\/usr\/bin\/env python3\$'" \
  && ok "shebang marker check present" \
  || bad "shebang marker check missing from the block"
echo "$BLOCK" | grep -q 'fail "could not download agent - check join token"' \
  && ok "fail message names the join token" \
  || bad "fail message does not name the join token"
echo "$BLOCK" | grep -q 'seq 1 6' \
  && ok "retry loop (seq 1 6) intact" \
  || bad "retry loop missing from the block"

# ---- functional driver: run the block with stubbed report/fail ----
report() { :; }
fail() { echo "$1" > "$T/fail_msg"; exit 1; }
export -f report fail
run_block() {
  rm -f "$T/fail_msg"
  export WDIR="$T/wd-$1"; mkdir -p "$WDIR"
  export JOIN_TOKEN="tok" MAIN_URL="http://x"
  FAKE_CURL_MODE="$1" bash "$T/block.sh"
  echo $? > "$T/rc"
}
rc_of() { cat "$T/rc"; }
failed_with_token_hint() { [ -f "$T/fail_msg" ] && grep -q "join token" "$T/fail_msg"; }

# happy path: real agent.py passes the marker
run_block good
if [ "$(rc_of)" = "0" ] && [ ! -f "$T/fail_msg" ]; then ok "happy path: exit 0, no fail"; else bad "happy path rc=$(rc_of)"; fi
if cmp -s "$WDIR/agent.py" "$REPO/wing/agent.py"; then ok "happy path: agent.py byte-identical to the real one"; else bad "happy path: agent.py altered"; fi
# the post-block sed still replaces the name placeholder on a good download
WNAME="testnode"; export WNAME
sed -i "s/__WING_NAME__/$WNAME/g" "$WDIR/agent.py"
if ! grep -q "__WING_NAME__" "$WDIR/agent.py" && grep -q "testnode" "$WDIR/agent.py"; then
  ok "post-block sed replaces __WING_NAME__"
else
  bad "post-block sed did not replace __WING_NAME__"
fi

# 403 with --fail: curl writes nothing, block retries then fails
run_block curl403
if [ "$(rc_of)" = "1" ]; then ok "403/--fail: exit 1"; else bad "403/--fail rc=$(rc_of)"; fi
failed_with_token_hint && ok "403/--fail: message names the join token" || bad "403/--fail: message misses the token hint"

# 403 body landing without --fail: marker rejects the JSON
run_block body403
if [ "$(rc_of)" = "1" ]; then ok "403-body: exit 1 (JSON rejected by marker)"; else bad "403-body rc=$(rc_of)"; fi
failed_with_token_hint && ok "403-body: message names the join token" || bad "403-body: message misses the token hint"

# proxy HTML error page served as 200: marker rejects it
run_block html
if [ "$(rc_of)" = "1" ]; then ok "html-page: exit 1 (HTML rejected by marker)"; else bad "html-page rc=$(rc_of)"; fi
failed_with_token_hint && ok "html-page: message names the join token" || bad "html-page: message misses the token hint"

# empty download: marker rejects it
run_block empty
[ "$(rc_of)" = "1" ] && ok "empty: exit 1" || bad "empty rc=$(rc_of)"

# wrong shebang (a non-python payload): marker rejects it
run_block badshebang
if [ "$(rc_of)" = "1" ]; then ok "bad-shebang: exit 1"; else bad "bad-shebang rc=$(rc_of)"; fi
failed_with_token_hint && ok "bad-shebang: message names the join token" || bad "bad-shebang: message misses the token hint"

# ---- file-level guards ----
grep -q "droidfarm" "$SRC" \
  && bad "legacy droidfarm ref in install-wing.sh" \
  || ok "no legacy droidfarm refs in install-wing.sh"
bash -n "$SRC" && ok "bash -n install-wing.sh clean" || bad "bash -n install-wing.sh failed"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
