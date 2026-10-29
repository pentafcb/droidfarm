#!/bin/bash
# test_watchdog_name_gate.sh — unit tests for df-watchdog.sh v16 (instance-name
# gate on the discovery block):
#   G1  all-valid list passes through unchanged, no WARN
#   G2  32-char name kept, 33-char name dropped
#   G3  traversal names dropped (../x, ../../tmp/evil, /abs, a/b)
#   G4  shell-metachar names dropped (x;y, $(id), `id`, a*b)
#   G5  mixed list: valid kept in order, invalid dropped
#   G6  all-invalid -> INSTANCES empty, WARN logged, no crash
#   G7  WARN is one line per cycle: count + first 5 names only (8 invalid in)
#   G8  WARN names the skipped keys (not just the count)
#   G9  empty input -> empty output, no WARN
#   G10 dash/underscore/digits valid (manager contract ^[A-Za-z0-9_-]{1,32}$)
#   G11 source contract: gate markers exist, gate sits after the rbx1-5
#       fallback and before the v15 section, log() defined before the gate,
#       both consumers (main loop + boot_recovery call) sit after the gate
#   G12 negative proof: without the gate, a traversal name survives (the
#       suite is not vacuous — it fails on the pre-gate script)
# Runs the REAL log() + the verbatim gate block against temp state. Hermetic.
# Exit 0 when all checks pass, 1 otherwise. Final line: PASS=N FAIL=M.
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../../waydroid-image/df-watchdog.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
LOG="$T/watchdog.log"

# ---- build the harness: real log() + verbatim gate block -------------------
{
  echo "LOG=\"$LOG\""
  sed -n '/^log() {/,/^}/p' "$SRC"
  echo 'run_gate() {'
  echo '  INSTANCES="$1"'
  sed -n '/# --- V16-NAME-GATE-START ---/,/# --- V16-NAME-GATE-END ---/p' "$SRC"
  echo '  printf "%s" "$INSTANCES"'
  echo '}'
} > "$T/gate.sh"
bash -n "$T/gate.sh" || { echo "harness syntax broken"; echo "PASS=0 FAIL=1"; exit 1; }

run() { # run <input> -> stdout = filtered list; log file holds WARN lines
  : > "$LOG"
  # shellcheck disable=SC1090
  out="$(. "$T/gate.sh"; run_gate "$1")"
  echo "$out"
}
warn_lines() { grep -c "skipping .* invalid instance name" "$LOG" || true; }

# G1: all-valid passes through, no WARN
out="$(run "rbx1 srt-2 a_b R9")"
[ "$out" = "rbx1 srt-2 a_b R9" ] && ok "G1 valid list unchanged" || bad "G1 valid list changed to [$out]"
[ "$(warn_lines)" = "0" ] && ok "G1 no WARN on valid input" || bad "G1 spurious WARN"

# G2: length boundary (manager contract: max 32)
n32="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"   # 32
n33="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # 33
out="$(run "$n32 $n33")"
[ "$out" = "$n32" ] && ok "G2 32 kept, 33 dropped" || bad "G2 boundary wrong: [$out]"

# G3: traversal dropped
out="$(run "rbx1 ../x ../../tmp/evil /abs a/b rbx2")"
[ "$out" = "rbx1 rbx2" ] && ok "G3 traversal names dropped" || bad "G3 traversal survived: [$out]"

# G4: shell metachars dropped
out="$(run 'rbx1 x;y $(id) `id` a*b rbx2')"
[ "$out" = "rbx1 rbx2" ] && ok "G4 metachar names dropped" || bad "G4 metachar survived: [$out]"

# G5: mixed order preserved
out="$(run "rbx3 ../bad rbx1 a;b rbx2")"
[ "$out" = "rbx3 rbx1 rbx2" ] && ok "G5 valid kept in order" || bad "G5 order wrong: [$out]"

# G6: all-invalid -> empty, WARN, no crash
out="$(run "../x a/b x;y")"
[ -z "$out" ] && ok "G6 all-invalid yields empty" || bad "G6 all-invalid not empty: [$out]"
[ "$(warn_lines)" = "1" ] && ok "G6 WARN logged on all-invalid" || bad "G6 WARN missing"

# G7: WARN is one line, count + first 5 only (8 invalid in)
run "../b1 ../b2 ../b3 ../b4 ../b5 ../b6 ../b7 ../b8" > /dev/null
[ "$(warn_lines)" = "1" ] && ok "G7 exactly one WARN line" || bad "G7 WARN line count wrong"
grep -q "skipping 8 invalid" "$LOG" && ok "G7 count is 8" || bad "G7 count wrong"
grep -q "\.\./b1 \.\./b2 \.\./b3 \.\./b4 \.\./b5" "$LOG" && ok "G7 first 5 named" || bad "G7 first 5 missing"
if grep -q "\.\./b6" "$LOG"; then bad "G7 6th name leaked into log"; else ok "G7 names truncated at 5"; fi

# G8: WARN names the skipped keys (not just the count)
run "../evil x;y" > /dev/null
grep -q "skipping 2 invalid" "$LOG" && grep -q '\.\./evil' "$LOG" && grep -q 'x;y' "$LOG" \
  && ok "G8 WARN names skipped keys" || bad "G8 WARN lacks names/count"

# G9: empty input -> empty, no WARN
out="$(run "")"
[ -z "$out" ] && [ "$(warn_lines)" = "0" ] && ok "G9 empty in, empty out, silent" \
  || bad "G9 empty-input misbehavior: [$out]"

# G10: dash/underscore/digits valid
out="$(run "a-1_b-2 9 Z-z_9")"
[ "$out" = "a-1_b-2 9 Z-z_9" ] && ok "G10 dash/underscore/digits valid" || bad "G10 valid chars dropped: [$out]"

# G11: source contracts (line ordering in the real script)
gate_start=$(grep -n "V16-NAME-GATE-START" "$SRC" | cut -d: -f1)
gate_end=$(grep -n "V16-NAME-GATE-END" "$SRC" | cut -d: -f1)
fallback=$(grep -n 'INSTANCES="rbx1 rbx2 rbx3 rbx4 rbx5"' "$SRC" | cut -d: -f1)
v15sec=$(grep -n '# --- v15: docker-daemon guard' "$SRC" | cut -d: -f1)
logdef=$(grep -n '^log() {' "$SRC" | cut -d: -f1)
[ -n "$gate_start" ] && [ -n "$gate_end" ] && [ "$gate_start" -lt "$gate_end" ] \
  && ok "G11 gate markers present and ordered" || bad "G11 gate markers missing/misordered"
[ "$fallback" -lt "$gate_start" ] && ok "G11 gate after rbx1-5 fallback" || bad "G11 gate before fallback"
[ "$gate_end" -lt "$v15sec" ] && ok "G11 gate before v15 section" || bad "G11 gate after v15 section"
[ "$logdef" -lt "$gate_start" ] && ok "G11 log() defined before gate" || bad "G11 log() after gate"
after_loops=$(awk -v gs="$gate_start" 'NR > gs && (/for i in \$INSTANCES; do \( trap/ || /^boot_recovery$/)' "$SRC" | wc -l)
[ "$after_loops" -ge 2 ] && ok "G11 both consumers after gate (main loop + boot_recovery call: $after_loops)" || bad "G11 consumers not all after gate"

# G12: negative proof — pre-gate behavior keeps the traversal name
pre="$(INSTANCES="rbx1 ../x"; printf "%s" "$INSTANCES")"
[ "$pre" = "rbx1 ../x" ] && ok "G12 pre-gate input keeps traversal (non-vacuous)" || bad "G12 negative proof broken"

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
