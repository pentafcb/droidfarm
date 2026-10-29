#!/bin/bash
# test_js_gate.sh — unit tests for check-js.sh (dashboard JS syntax gate).
# Hermetic: synthetic pages + the real repo index.html, no network.
# Exit 0 when all checks pass, 1 otherwise.
HERE="$(cd "$(dirname "$0")" && pwd)"
GATE="$HERE/check-js.sh"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  ok: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

run_gate() { # $1 = html file ; echoes output; returns gate exit code
    JSGATE_HTML="$1" bash "$GATE" > "$T/out.log" 2>&1
    echo $?
}

mkpage() { # $1 = name, $2.. = script-tag lines -> writes $T/$1.html
    local name="$1"; shift
    { echo "<!doctype html><html><head><title>t</title></head><body><h1>hi</h1>";
      printf '%s\n' "$@";
      echo "</body></html>"; } > "$T/$name.html"
}

# --- 1: the real dashboard page parses ---------------------------------------
code="$(run_gate "$HERE/../../manager/static/index.html")"
out="$(cat "$T/out.log")"
if [ "$code" -eq 0 ] && echo "$out" | grep -q "PASS: 1 inline"; then ok "real index.html passes (1 block)"; else bad "real index.html should pass (code=$code)"; fi

# --- 2: regression — the exact defect class from the c7db022 incident --------
# c7db022 deleted a catch, leaving try{} with no catch -> V8:
# "SyntaxError: Missing catch or finally after try", which killed the whole
# dashboard script block (no buttons worked). The gate must catch this.
mkpage broken "  <script>" "document.getElementById('x').addEventListener('click',()=>{" "  try{ doLogin(); }" "});" "  </script>"
code="$(run_gate "$T/broken.html")"
out="$(cat "$T/out.log")"
if [ "$code" -ne 0 ] && echo "$out" | grep -q "Missing catch or finally"; then ok "try-without-catch fails gate (incident regression)"; else bad "try-without-catch must fail (code=$code)"; fi

# --- 3: missing node -> warning-only skip ------------------------------------
code="$(JSGATE_NODE="/nonexistent-node-xyz" JSGATE_HTML="$T/broken.html" bash "$GATE" > "$T/out.log" 2>&1; echo $?)"
out="$(cat "$T/out.log")"
if [ "$code" -eq 0 ] && echo "$out" | grep -q "WARN"; then ok "missing node = warning-only skip"; else bad "missing node must warn+pass (code=$code)"; fi

# --- 4: external <script src=...> is ignored ----------------------------------
mkpage extsrc '  <script src="https://example.com/does-not-exist.js"></script>' "  <script>" "var x = 1;" "  </script>"
code="$(run_gate "$T/extsrc.html")"
if [ "$code" -eq 0 ]; then ok "external src script ignored"; else bad "src script must be ignored (code=$code)"; fi

# --- 5: multi-block page names the broken block -------------------------------
mkpage multi "  <script>" "var good = 1;" "  </script>" "  <script>" "var oops = ;" "  </script>"
code="$(run_gate "$T/multi.html")"
out="$(cat "$T/out.log")"
if [ "$code" -ne 0 ] && echo "$out" | grep -q "block 2"; then ok "block 2 named in multi-block failure"; else bad "multi-block failure must name block 2 (code=$code)"; fi

# --- 6: missing HTML file -> fail closed ---------------------------------------
code="$(run_gate "$T/no-such-file.html")"
if [ "$code" -ne 0 ]; then ok "missing HTML fails closed"; else bad "missing HTML must fail"; fi

# --- 7: zero inline blocks -> fail closed (gate can't verify) ------------------
mkpage noblocks "<p>no scripts here</p>"
code="$(run_gate "$T/noblocks.html")"
out="$(cat "$T/out.log")"
if [ "$code" -ne 0 ] && echo "$out" | grep -q "no inline"; then ok "zero blocks fails closed"; else bad "zero blocks must fail closed (code=$code)"; fi

# --- 8: unterminated <script> (no </script>) -> fail closed --------------------
{ echo "<html><body>"; echo "  <script>"; echo "var x = 1;"; echo "</body></html>"; } > "$T/unterminated.html"
code="$(run_gate "$T/unterminated.html")"
if [ "$code" -ne 0 ]; then ok "unterminated script fails closed"; else bad "unterminated script must fail"; fi

# --- 9: case-insensitive SCRIPT tag --------------------------------------------
{ echo "<html><body>"; echo "  <SCRIPT>"; echo "var y = 2;"; echo "  </SCRIPT>"; echo "</body></html>"; } > "$T/upper.html"
code="$(run_gate "$T/upper.html")"
if [ "$code" -eq 0 ]; then ok "uppercase SCRIPT tag handled"; else bad "uppercase SCRIPT must pass (code=$code)"; fi

# --- 10: src= with single quotes also ignored -----------------------------------
mkpage sq "  <script src='x.js'>" "var broken = ;" "  </script>" "  <script>" "var fine = 3;" "  </script>"
code="$(run_gate "$T/sq.html")"
if [ "$code" -eq 0 ]; then ok "single-quoted src ignored"; else bad "single-quoted src must be ignored (code=$code)"; fi

echo "js-gate: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
