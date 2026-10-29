#!/usr/bin/env bash
# fetch-roblox-apks.sh — download the pinned Roblox 2.740.931 split APKs
# directly on the VPS and verify every byte against the pinned sha256
# values before they reach the docker build context.
#
# RUN ON THE VPS AS ROOT:
#   bash fetch-roblox-apks.sh <base-apk-url> <x86_64-apk-url> [dest-dir]
#
# dest-dir defaults to /root/jerusalem/waydroid-image (the docker build
# context — vps-image-build.sh picks the APKs up from there, so with the
# default dest the next step is just `bash vps-image-build.sh`).
#
# WHY THE SHA256 PIN: the download links expire (the 2026-10-15 batch) and
# an expired/dead link serves an HTML error page instead of the APK.
# curl --fail catches HTTP errors, but a 200-served "link expired" page
# would sail through and get baked into the fleet image. The pin fails
# closed on ANY byte difference: the staged file is deleted and the run
# aborts with the likely cause (expired link — mint fresh links and retry).
#
# Fail-closed, idempotent (skips a download whose staged file already
# matches the pin; re-downloads on mismatch). Never touches instance
# data, volumes, or running containers. Logs carry names/sizes/sha256
# only — the download URLs are NEVER printed (they may carry
# query-string tokens).
#
# DF_PIN_BASE_SHA / DF_PIN_X86_SHA override the pins. TEST-ONLY (the
# hermetic suite uses small fixture zips); in production the compiled-in
# pins are the whole point of this script.
set -u

PIN_BASE_SHA_DEFAULT="114a6f8ed6f0f974cf632a9cbec2f5cd45e1d85d7ed251db4f3920140bc6ead4"
PIN_X86_SHA_DEFAULT="5ba61d4404ac8cd92636318a5e7ef83520f3113670b6b57f31bcff55e581bdc0"
PIN_BASE_SHA="${DF_PIN_BASE_SHA:-$PIN_BASE_SHA_DEFAULT}"
PIN_X86_SHA="${DF_PIN_X86_SHA:-$PIN_X86_SHA_DEFAULT}"

fail() { echo "[fetch-roblox-apks] FAIL: $*" >&2; exit 1; }
ok()   { echo "[fetch-roblox-apks] $*"; }

[ $# -ge 2 ] && [ $# -le 3 ] || {
    echo "usage: bash fetch-roblox-apks.sh <base-apk-url> <x86_64-apk-url> [dest-dir]" >&2
    exit 2
}
BASE_URL="$1"; X86_URL="$2"; DEST="${3:-/root/jerusalem/waydroid-image}"

[ "$(id -u)" = "0" ] || fail "run as root"
command -v curl >/dev/null 2>&1 || fail "curl not found on PATH"
command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH (needed for zip sanity check)"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum not found on PATH"

mkdir -p "$DEST" || fail "cannot create dest dir: $DEST"
[ -d "$DEST" ] || fail "dest is not a directory: $DEST"

sha_of() { sha256sum "$1" | cut -d' ' -f1; }

zip_has_base_entries() {
    python3 - "$1" <<'EOF' >/dev/null 2>&1
import sys, zipfile
try:
    names = zipfile.ZipFile(sys.argv[1]).namelist()
except Exception:
    sys.exit(1)
sys.exit(0 if "AndroidManifest.xml" in names else 1)
EOF
}

zip_has_x86_entries() {
    python3 - "$1" <<'EOF' >/dev/null 2>&1
import sys, zipfile
try:
    names = zipfile.ZipFile(sys.argv[1]).namelist()
except Exception:
    sys.exit(1)
sys.exit(0 if any(n.startswith("lib/x86_64/") and n.endswith(".so") for n in names) else 1)
EOF
}

# fetch_one <role: base|x86_64> <url> <dest-file> <pin-sha> <zip-check-fn>
fetch_one() {
    local role="$1" url="$2" destfile="$3" pin="$4" zipcheck="$5"
    local tmp="$DEST/.fetch-roblox-${role}.tmp.$$"

    if [ -f "$destfile" ] && [ "$(sha_of "$destfile")" = "$pin" ]; then
        ok "$role already pinned at $destfile (skipping download)"
        return 0
    fi
    # A staged file that does NOT match the pin is never trusted.
    [ -f "$destfile" ] && ok "$role staged file failed the pin — re-downloading"

    trap 'rm -f "$tmp"' EXIT
    ok "downloading $role ..."
    if ! curl --fail --location --retry 3 --retry-all-errors \
            --connect-timeout 20 --max-time 1800 -sS -o "$tmp" "$url"; then
        rm -f "$tmp"; trap - EXIT
        fail "$role download failed (curl error) — check the link and retry"
    fi
    [ -s "$tmp" ] || { rm -f "$tmp"; trap - EXIT; fail "$role downloaded empty file"; }

    local got; got="$(sha_of "$tmp")"
    if [ "$got" != "$pin" ]; then
        rm -f "$tmp"; trap - EXIT
        fail "$role bytes do not match the pinned 2.740.931 split (got sha256 $got) — the download link has likely EXPIRED (it served something else, e.g. an HTML error page). Mint fresh links and retry; nothing was staged."
    fi

    if ! "$zipcheck" "$tmp"; then
        rm -f "$tmp"; trap - EXIT
        fail "$role failed the zip sanity check (expected APK entries missing)"
    fi

    mv -f "$tmp" "$destfile" || { rm -f "$tmp"; trap - EXIT; fail "cannot stage $role into $DEST"; }
    trap - EXIT
    ok "$role pinned: $destfile size=$(stat -c%s "$destfile") sha256=$pin"
}

fetch_one "base"   "$BASE_URL" "$DEST/roblox-base.apk"   "$PIN_BASE_SHA" zip_has_base_entries
fetch_one "x86_64" "$X86_URL"  "$DEST/roblox-x86_64.apk" "$PIN_X86_SHA"  zip_has_x86_entries

ok "both splits staged in $DEST — next: APK_SRC=$DEST bash vps-image-build.sh"
