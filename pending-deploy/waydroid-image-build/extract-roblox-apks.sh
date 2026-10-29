#!/usr/bin/env bash
# extract-roblox-apks.sh — extract the pinned Roblox 2.740.931 split APKs from
# the local XAPK backup into build-ready roblox-base.apk / roblox-x86_64.apk.
#
# Runs on the repo machine (any session with the workspace). The 235MB XAPK is
# the recovery copy at ~/workspace/your_files/droidfarm-apk-backup/ — Roblox
# dropped x86_64 from newer builds, so this file is irreplaceable; this script
# only READS it and writes the two small splits to the output dir.
#
#   ROBLOX_XAPK  path to the .xapk (default: the workspace backup)
#   APK_OUT      output dir (default: ./roblox-apks)
#
# Fail-closed: missing XAPK, missing archive entries, corrupt zips, a split
# without x86_64 native libs, or a base without AndroidManifest.xml all abort
# with a reason. Logs carry names/sizes/sha256 only — never file contents.
set -u

XAPK="${ROBLOX_XAPK:-$HOME/workspace/your_files/droidfarm-apk-backup/roblox-2.740.931-x86_64.xapk}"
OUT="${APK_OUT:-./roblox-apks}"
BASE_ENTRY="com.roblox.client.apk"
SPLIT_ENTRY="config.x86_64.apk"

fail() { echo "[extract-roblox-apks] FAIL: $*" >&2; exit 1; }
ok()   { echo "[extract-roblox-apks] $*"; }

command -v unzip >/dev/null 2>&1 || fail "unzip not found on PATH"
[ -f "$XAPK" ] || fail "XAPK not found: $XAPK"
mkdir -p "$OUT" || fail "cannot create output dir: $OUT"
[ -w "$OUT" ] || fail "output dir not writable: $OUT"

# Entry selection is exact — the XAPK also carries arm64/arm32 config splits.
for e in "$BASE_ENTRY" "$SPLIT_ENTRY"; do
    unzip -Z1 "$XAPK" 2>/dev/null | grep -qx "$e" \
        || fail "entry missing in XAPK: $e"
done

unzip -o -j -q "$XAPK" "$BASE_ENTRY" -d "$OUT" \
    || fail "failed extracting $BASE_ENTRY"
mv -f "$OUT/$BASE_ENTRY" "$OUT/roblox-base.apk" \
    || fail "rename to roblox-base.apk failed"

unzip -o -j -q "$XAPK" "$SPLIT_ENTRY" -d "$OUT" \
    || fail "failed extracting $SPLIT_ENTRY"
mv -f "$OUT/$SPLIT_ENTRY" "$OUT/roblox-x86_64.apk" \
    || fail "rename to roblox-x86_64.apk failed"

# Both outputs must be intact APKs: valid zip, base has a manifest,
# x86_64 split carries real x86_64 native libs (guards against grabbing an
# empty config split or a mislabeled arm build).
unzip -t -q "$OUT/roblox-base.apk" >/dev/null \
    || fail "roblox-base.apk is not a valid zip"
unzip -Z1 "$OUT/roblox-base.apk" | grep -qx "AndroidManifest.xml" \
    || fail "roblox-base.apk has no AndroidManifest.xml"
unzip -t -q "$OUT/roblox-x86_64.apk" >/dev/null \
    || fail "roblox-x86_64.apk is not a valid zip"
unzip -Z1 "$OUT/roblox-x86_64.apk" | grep -q '^lib/x86_64/.*\.so$' \
    || fail "roblox-x86_64.apk has no lib/x86_64/*.so native libs"

for f in roblox-base.apk roblox-x86_64.apk; do
    sz=$(stat -c%s "$OUT/$f")
    sha=$(sha256sum "$OUT/$f" | cut -d' ' -f1)
    ok "$f size=$sz sha256=$sha"
done
ok "extracted 2 APKs -> $OUT (base + x86_64 split, pinned 2.740.931)"
