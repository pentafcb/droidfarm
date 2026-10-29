#!/bin/bash
# check-js.sh — dashboard JS syntax gate for the deploy preflight.
#
# WHY THIS EXISTS: commit c7db022 shipped an index.html whose single inline
# <script> block had a try{} with the catch accidentally deleted. A JS syntax
# error kills the ENTIRE script block, so Penta's dashboard rendered but NO
# button worked ("when I click login nothing happen"). This gate runs
# `node --check` over every inline <script> block in manager/static/index.html
# and fails closed — a broken dashboard can never reach the VPS again.
#
# Extraction is a plain regex over the HTML. Browsers terminate a script
# block at the first </script>, so the gate uses the same rule: a literal
# </script> inside a JS string would break in the browser too.
#
# Env: REPO        (default: repo root derived from this script's location)
#      JSGATE_HTML (default: $REPO/manager/static/index.html)
#      JSGATE_NODE (default: node)
# Exit 0 = all inline blocks parse; 1 = syntax error, missing file, or no
# blocks found (fail closed). Missing node = warning-only skip (exit 0),
# consistent with the ffmpeg warning-only rule in vps-apply.sh.
set -u

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="${REPO:-$(cd "$SELF_DIR/../.." && pwd)}"
HTML="${JSGATE_HTML:-$REPO/manager/static/index.html}"
NODE="${JSGATE_NODE:-node}"

fail() { echo "[js-gate] FAIL: $*" >&2; exit 1; }

[ -f "$HTML" ] || fail "dashboard file not found: $HTML"

if ! command -v "$NODE" >/dev/null 2>&1; then
    echo "[js-gate] WARN: node not found ($NODE) — JS syntax check skipped (warning-only)"
    exit 0
fi

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

# Extract inline <script> blocks (no src attribute), one file per block.
n="$(JSGATE_HTML="$HTML" OUTDIR="$T" python3 - <<'EOF'
import os, re, sys
html = open(os.environ["JSGATE_HTML"], encoding="utf-8").read()
outdir = os.environ["OUTDIR"]
blocks = []
for m in re.finditer(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
                     html, re.S | re.I):
    if re.search(r"""\bsrc\s*=""", m.group("attrs"), re.I):
        continue  # external script — not shipped inline, nothing to check
    blocks.append(m.group("body"))
for i, body in enumerate(blocks, 1):
    with open(os.path.join(outdir, "block-%d.js" % i), "w",
                           encoding="utf-8") as f:
        f.write(body)
print(len(blocks))
EOF
)" || fail "script-block extraction crashed"

[ "$n" -gt 0 ] 2>/dev/null || fail "no inline <script> blocks found in $HTML (gate cannot verify — update the gate)"

bad=0
count=0
for f in "$T"/block-*.js; do
    num="${f##*/block-}"; num="${num%.js}"
    count=$((count + 1))
    if ! out="$("$NODE" --check "$f" 2>&1)"; then
        echo "[js-gate] FAIL: block $num has a JS syntax error:"
        echo "$out" | sed 's/^/[js-gate]   /'
        bad=$((bad + 1))
    else
        echo "[js-gate] ok: block $num parses"
    fi
done

[ "$bad" -eq 0 ] || fail "$bad of $count inline <script> block(s) failed node --check"
echo "[js-gate] PASS: $count inline <script> block(s) parse cleanly"
