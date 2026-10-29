#!/bin/bash
# test_wing_installer_staging.sh — hermetic tests for staging the wing
# node installers (install-wing.sh + install-wing.bat) in apply-all.
#
# Context: the manager serves all three wing downloads from WING_DIR on
# disk (/api/install/agent.py, /api/install/install-wing.sh,
# /api/install/install-wing.bat, placeholders replaced at serve time),
# but apply-all only staged agent.py. The installer fixes (e.g. the
# marker-verified agent.py download from wing-installer-agent-check)
# had no path to the VPS except a hand-rolled scp. This suite wires the
# two installers into the deploy queue: SOURCES/STAGE_NAMES/KEYS set
# agreement, $DF_DIR/wing target mapping, serve-time placeholder hygiene
# (no hardcoded real join token — the 2026-10-06 security-fix class),
# bash -n gates, and the live-verify --ssh probes.
#
# No network, no VPS. Final line: PASS=$PASS FAIL=$FAIL (preflight t90
# greps it).
set -u
S="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$S/../.." && pwd)"
DA="$REPO/pending-deploy/apply-all/deploy-all.sh"
VA="$REPO/pending-deploy/apply-all/vps-apply.sh"
LV="$REPO/pending-deploy/live-verify/verify-live.sh"
APP="$REPO/manager/app.py"
SH="$REPO/wing/install-wing.sh"
BAT="$REPO/wing/install-wing.bat"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "ok: $1"; }
bad() { FAIL=$((FAIL+1)); echo "BAD: $1"; }

SOURCES="$(sed -n 's/^SOURCES="\(.*\)"/\1/p' "$DA")"
STAGE_NAMES="$(sed -n 's/^STAGE_NAMES="\(.*\)"/\1/p' "$DA")"
KEYS="$(sed -n 's/^KEYS="\(.*\)"/\1/p' "$VA")"

# --- wiring sets ---
[ -n "$SOURCES" ] && [ -n "$STAGE_NAMES" ] && [ -n "$KEYS" ] \
  && ok "SOURCES/STAGE_NAMES/KEYS parse" || bad "could not parse the wiring lists"
n_src="$(printf '%s' "$SOURCES" | wc -w)"; n_names="$(printf '%s' "$STAGE_NAMES" | wc -w)"
n_keys="$(printf '%s' "$KEYS" | wc -w)"
[ "$n_src" -eq "$n_names" ] && [ "$n_names" -eq "$n_keys" ] \
  && ok "list counts agree ($n_src)" || bad "count mismatch SOURCES=$n_src STAGE_NAMES=$n_names KEYS=$n_keys"
# order alignment: build_stage pairs SOURCES[i] -> STAGE_NAMES[i] by position
i=0; mis=0
for src in $SOURCES; do
  i=$((i + 1))
  name="$(printf '%s' "$STAGE_NAMES" | cut -d' ' -f"$i")"
  [ "$(basename "$src")" = "$name" ] || mis=$((mis + 1))
  [ -f "$REPO/$src" ] || mis=$((mis + 1))
done
[ "$mis" -eq 0 ] && ok "positional basename pairing + every source exists" || bad "positional pairing/source-existence failures: $mis"
# set agreement (order-insensitive)
diff <(printf '%s' "$STAGE_NAMES" | tr ' ' '\n' | sort) <(printf '%s' "$KEYS" | tr ' ' '\n' | sort) >/dev/null \
  && ok "STAGE_NAMES == KEYS (set)" || bad "STAGE_NAMES/KEYS sets differ"
case " $SOURCES " in *" wing/install-wing.sh "*) ok "SOURCES has wing/install-wing.sh";; *) bad "SOURCES missing wing/install-wing.sh";; esac
case " $SOURCES " in *" wing/install-wing.bat "*) ok "SOURCES has wing/install-wing.bat";; *) bad "SOURCES missing wing/install-wing.bat";; esac
case " $STAGE_NAMES " in *" install-wing.sh "*) ok "STAGE_NAMES has install-wing.sh";; *) bad "STAGE_NAMES missing install-wing.sh";; esac
case " $STAGE_NAMES " in *" install-wing.bat "*) ok "STAGE_NAMES has install-wing.bat";; *) bad "STAGE_NAMES missing install-wing.bat";; esac

# --- target mapping (extract target_for verbatim from vps-apply.sh) ---
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
sed -n '/^target_for() {/,/^}/p' "$VA" > "$T/target_for.sh"
[ -s "$T/target_for.sh" ] && ok "extracted target_for from vps-apply.sh" || bad "could not extract target_for"
# shellcheck disable=SC1090
DF_DIR="/root/jerusalem"; WATCHDOG_DST="/usr/local/bin/df-watchdog.sh"
. "$T/target_for.sh"
t="$(target_for install-wing.sh)"
[ "$t" = "/root/jerusalem/wing/install-wing.sh" ] && ok "install-wing.sh -> /root/jerusalem/wing/install-wing.sh" || bad "install-wing.sh maps to '$t'"
t="$(target_for install-wing.bat)"
[ "$t" = "/root/jerusalem/wing/install-wing.bat" ] && ok "install-wing.bat -> /root/jerusalem/wing/install-wing.bat" || bad "install-wing.bat maps to '$t'"
ta="$(target_for agent.py)"
[ "$(dirname "$t")" = "$(dirname "$ta")" ] && ok "installers land in the same WING_DIR as agent.py" || bad "installer dir $(dirname "$t") != agent dir $(dirname "$ta")"
t="$(target_for no-such-file)"
[ -z "$t" ] && ok "unknown stage key maps to empty (fail-closed)" || bad "unknown key maps to '$t'"

# --- placeholder / token hygiene (never print values) ---
grep -q '__JOIN_TOKEN__' "$SH" && ok ".sh carries the __JOIN_TOKEN__ placeholder" || bad ".sh missing __JOIN_TOKEN__"
grep -q '__MAIN_URL__' "$SH" && ok ".sh carries the __MAIN_URL__ placeholder" || bad ".sh missing __MAIN_URL__"
grep -q '__JOIN_TOKEN__' "$BAT" && ok ".bat carries the __JOIN_TOKEN__ placeholder" || bad ".bat missing __JOIN_TOKEN__"
grep -q '__MAIN_URL__' "$BAT" && ok ".bat carries the __MAIN_URL__ placeholder" || bad ".bat missing __MAIN_URL__"
# every JOIN_TOKEN assignment must be the placeholder — a hardcoded real
# token here would ship a stale token to every new node (2026-10-06 class)
n_assign="$(grep -c '^JOIN_TOKEN=' "$SH")"
n_place="$(grep -c '^JOIN_TOKEN=__JOIN_TOKEN__$' "$SH")"
[ "$n_assign" -ge 1 ] && [ "$n_assign" -eq "$n_place" ] \
  && ok ".sh: all $n_assign JOIN_TOKEN= assignments are the placeholder" \
  || bad ".sh: $n_assign assignments, only $n_place placeholder (possible hardcoded token)"
n_assign="$(grep -c '^set "JOIN_TOKEN=' "$BAT")"
n_place="$(grep -c '^set "JOIN_TOKEN=__JOIN_TOKEN__"$' "$BAT")"
[ "$n_assign" -ge 1 ] && [ "$n_assign" -eq "$n_place" ] \
  && ok ".bat: all $n_assign set \"JOIN_TOKEN= assignments are the placeholder" \
  || bad ".bat: $n_assign assignments, only $n_place placeholder (possible hardcoded token)"

# --- bash -n gates ---
grep -q 'bash -n "\$REPO/wing/install-wing.sh"' "$DA" \
  && ok "deploy-all.sh preflight bash -n's wing/install-wing.sh" || bad "deploy-all.sh preflight missing the install-wing.sh syntax gate"
grep -q 'bash -n "\$DF_DIR/wing/install-wing.sh"' "$VA" \
  && ok "vps-apply.sh syntax_check bash -n's the deployed install-wing.sh" || bad "vps-apply.sh missing the install-wing.sh syntax gate"
if grep -q 'bash -n.*install-wing\.bat' "$DA" "$VA"; then
  bad "install-wing.bat in a bash -n gate (it is not shell — the gate would fail spuriously)"
else
  ok "install-wing.bat excluded from bash -n gates (not shell)"
fi
bash -n "$SH" && ok "bash -n clean on the real install-wing.sh" || bad "bash -n fails on wing/install-wing.sh"

# --- manager serve contract ---
grep -q '"/api/install/"' "$APP" && ok "app.py has the /api/install/ route" || bad "app.py missing the /api/install/ route"
grep -q '("install-wing.sh", "install-wing.bat")' "$APP" \
  && ok "route whitelists install-wing.sh + install-wing.bat" || bad "installer whitelist tuple missing/drifted"
grep -q 'os.path.join(WING_DIR, fname)' "$APP" \
  && ok "installers served from WING_DIR on disk (per request, no restart)" || bad "WING_DIR serve contract drifted"
grep -q '\.replace("__JOIN_TOKEN__"' "$APP" \
  && ok "placeholders replaced at serve time" || bad "serve-time placeholder replacement missing"

# --- live-verify --ssh probes ---
probe_list="$(sed -n 's/^for f in \(.*\); do$/\1/p' "$LV")"
case " $probe_list " in *" wing/install-wing.sh "*) ok "--ssh for-list probes wing/install-wing.sh";; *) bad "--ssh for-list missing wing/install-wing.sh";; esac
case " $probe_list " in *" wing/install-wing.bat "*) ok "--ssh for-list probes wing/install-wing.bat";; *) bad "--ssh for-list missing wing/install-wing.bat";; esac
# key derivation rule: SHA_$(path | tr '/.' '__')
k_sh="$(printf 'wing/install-wing.sh' | tr '/.' '__')"
k_bat="$(printf 'wing/install-wing.bat' | tr '/.' '__')"
grep -q "check_file \"wing/install-wing.sh\" *\"SHA_$k_sh\"" "$LV" \
  && ok "check_file probes wing/install-wing.sh with key SHA_$k_sh" || bad "check_file line for install-wing.sh missing/wrong key"
grep -q "check_file \"wing/install-wing.bat\" *\"SHA_$k_bat\"" "$LV" \
  && ok "check_file probes wing/install-wing.bat with key SHA_$k_bat" || bad "check_file line for install-wing.bat missing/wrong key"

# --- hygiene ---
if grep -qi "droidfarm" "$SH" "$BAT"; then
  bad "legacy droidfarm ref in an installer (would trip queue-audit check 6/7)"
else
  ok "no legacy droidfarm refs in either installer"
fi

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
