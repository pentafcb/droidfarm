#!/usr/bin/env bash
# vps-image-build.sh — build waydroid-instance:latest on the VPS.
#
# RUN ON THE VPS AS ROOT, after the two APKs from extract-roblox-apks.sh have
# been scp'd over (see DEPLOY.md). Copies the APKs into the build context at
# /root/jerusalem/waydroid-image/ and runs the docker build.
#
#   IMAGE_CTX  build context dir (default /root/jerusalem/waydroid-image)
#   APK_SRC    dir holding roblox-base.apk + roblox-x86_64.apk (default: .)
#
# Fail-closed: refuses to build when the context is incomplete, the Dockerfile
# COPY lines are missing, an APK is absent, or a staged APK fails the pinned
# sha256 / arch checks — a half-built or wrong-APK image must never become
# the fleet's base. Idempotent: re-running rebuilds cleanly.
# Never deletes instance data, volumes, or containers. Logs carry
# names/sizes/sha256 only — never secrets or file contents.
set -u

CTX="${IMAGE_CTX:-/root/jerusalem/waydroid-image}"
SRC="${APK_SRC:-.}"

fail() { echo "[vps-image-build] FAIL: $*" >&2; exit 1; }
ok()   { echo "[vps-image-build] $*"; }

[ "$(id -u)" = "0" ] || fail "run as root"
command -v docker >/dev/null 2>&1 || fail "docker not found on PATH"

[ -d "$CTX" ] || fail "build context missing: $CTX (scp the waydroid-image dir first)"
[ -f "$CTX/Dockerfile" ] || fail "no Dockerfile in $CTX"

# The Dockerfile's single-source COPY lines are the build's real dependency
# set. Derive them from the Dockerfile itself so this preflight can never go
# stale when a helper script is added to the image (the 2026-10-20 drift:
# android-optimize.sh and boot-complete.sh gained Dockerfile COPY lines after
# the old hard-coded helper list was written — a context missing either
# passed preflight and died at `docker build` with a cryptic "COPY failed:
# file not found"). The two APKs are staged from APK_SRC below, so their COPY
# lines are checked for presence only. Fail closed on any COPY form this
# parser does not understand (multi-source, globs) rather than misparse it.
COPY_PAIRS=""
while IFS= read -r copyline; do
    rest="${copyline#COPY }"
    while [ "${rest#--}" != "$rest" ]; do rest="${rest#* }"; done  # strip --flags
    set -f; set -- $rest; set +f
    # shellcheck disable=SC2124
    [ "$#" = 2 ] || fail "Dockerfile has unsupported COPY form: '$copyline' (single-source COPY only)"
    src="$1"; dest="$2"
    case "$src" in
        *\** | */*) fail "Dockerfile COPY source is not a plain filename: '$src'" ;;
    esac
    case "$dest" in
        /*) ;;
        *) fail "Dockerfile COPY destination is not absolute: '$dest'" ;;
    esac
    COPY_PAIRS="$COPY_PAIRS $src:$dest"
done < <(grep -E '^COPY ' "$CTX/Dockerfile" || true)
[ -n "$COPY_PAIRS" ] || fail "no COPY lines parsed from $CTX/Dockerfile"

for pair in $COPY_PAIRS; do
    src="${pair%%:*}"
    case "$src" in
        roblox-base.apk|roblox-x86_64.apk) continue ;;  # staged from APK_SRC below
    esac
    [ -f "$CTX/$src" ] || fail "build context missing $src (Dockerfile COPYs it into the image)"
done
case " $COPY_PAIRS " in
    *" roblox-base.apk:"*) ;;
    *) fail "Dockerfile has no COPY roblox-base.apk line" ;;
esac
case " $COPY_PAIRS " in
    *" roblox-x86_64.apk:"*) ;;
    *) fail "Dockerfile has no COPY roblox-x86_64.apk line" ;;
esac

for f in roblox-base.apk roblox-x86_64.apk; do
    [ -f "$SRC/$f" ] || fail "APK missing: $SRC/$f (run extract-roblox-apks.sh first)"
    [ -s "$SRC/$f" ] || fail "APK empty: $SRC/$f"
done

# Pinned Roblox 2.740.931 split SHAs — MUST stay identical to
# fetch-roblox-apks.sh's PIN_BASE_SHA_DEFAULT / PIN_X86_SHA_DEFAULT
# (queue-audit check 10 fails the deploy on any disagreement).
# DF_PIN_BASE_SHA / DF_PIN_X86_SHA override the pins (test seam, same
# pattern as the fetch script; also the escape hatch for a deliberate
# pin move after Penta approves it — a mismatch still fails closed).
PIN_BASE_SHA_DEFAULT="114a6f8ed6f0f974cf632a9cbec2f5cd45e1d85d7ed251db4f3920140bc6ead4"
PIN_X86_SHA_DEFAULT="5ba61d4404ac8cd92636318a5e7ef83520f3113670b6b57f31bcff55e581bdc0"
PIN_BASE_SHA="${DF_PIN_BASE_SHA:-$PIN_BASE_SHA_DEFAULT}"
PIN_X86_SHA="${DF_PIN_X86_SHA:-$PIN_X86_SHA_DEFAULT}"
# DF_UNZIP_BIN overrides the unzip binary (test seam; default unzip).
UNZIP_BIN="${DF_UNZIP_BIN:-unzip}"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum not found on PATH"
command -v "$UNZIP_BIN" >/dev/null 2>&1 \
    || fail "$UNZIP_BIN not found on PATH (apt-get install -y unzip) - the APK arch check needs it"

# The splits may arrive via scp (Path B), bypassing BOTH the fetch script's
# sha256-pin check and the extract script's arch check. Verify here, before
# anything is baked into the fleet image: a stale, swapped, or mislabeled
# file fails closed now instead of shipping a broken Roblox to every new
# instance (which would only surface when the login taps fail).
for f in roblox-base.apk roblox-x86_64.apk; do
    _want="$PIN_X86_SHA"; [ "$f" = "roblox-base.apk" ] && _want="$PIN_BASE_SHA"
    _got="$(sha256sum "$SRC/$f" | cut -d' ' -f1)"
    [ "$_got" = "$_want" ] \
        || fail "$f sha256 mismatch: not the pinned 2.740.931 split (scp'd the wrong file?)"
    ok "$f sha256 matches the pinned 2.740.931 split"
done
# Arch sanity — same gates as extract-roblox-apks.sh: the base must be a
# real APK zip carrying AndroidManifest.xml; the config split must carry
# x86_64 native libs (an arm64-only split here means a mislabeled file).
"$UNZIP_BIN" -Z1 "$SRC/roblox-base.apk" 2>/dev/null | grep -qx "AndroidManifest.xml" \
    || fail "roblox-base.apk has no AndroidManifest.xml (not a valid base APK?)"
"$UNZIP_BIN" -Z1 "$SRC/roblox-x86_64.apk" 2>/dev/null | grep -q '^lib/x86_64/.*\.so$' \
    || fail "roblox-x86_64.apk has no lib/x86_64/*.so native libs (wrong-arch split?)"

cp -f "$SRC/roblox-base.apk" "$CTX/roblox-base.apk" \
    || fail "copy roblox-base.apk into context failed"
cp -f "$SRC/roblox-x86_64.apk" "$CTX/roblox-x86_64.apk" \
    || fail "copy roblox-x86_64.apk into context failed"
for f in roblox-base.apk roblox-x86_64.apk; do
    ok "staged $f size=$(stat -c%s "$CTX/$f") sha256=$(sha256sum "$CTX/$f" | cut -d' ' -f1)"
done

ok "docker build -t waydroid-instance:latest $CTX ..."
# Free-space preflight: the image build needs gigabytes in Docker's data-root
# (base image pull + build deps + ~150MB of APKs + layer overhead). A full
# disk fails `docker build` midway with a cryptic "no space left on device"
# after minutes of work — fail closed here instead, naming the fix. When
# free space cannot be determined, warn and continue: the build itself fails
# loudly on a truly full disk, so an unreadable df must never block a
# healthy build.
# VIB_MIN_FREE_GB overrides the floor (default 5); VIB_DOCKER_ROOT overrides
# the data-root discovery (default: `docker info`, fallback /var/lib/docker).
case "${VIB_MIN_FREE_GB:-5}" in ''|*[!0-9]*) _vib_min_gb=5 ;; *) _vib_min_gb="${VIB_MIN_FREE_GB:-5}" ;; esac
_vib_root="${VIB_DOCKER_ROOT:-$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)}"
[ -d "$_vib_root" ] || _vib_root="/"
_vib_free_kb="$(df -k --output=avail "$_vib_root" 2>/dev/null | tail -n 1 | tr -cd '0-9')"
if [ -n "$_vib_free_kb" ]; then
    if [ "$_vib_free_kb" -lt $(( _vib_min_gb * 1024 * 1024 )) ]; then
        fail "only $((_vib_free_kb / 1024))MB free under $_vib_root (need >= ${_vib_min_gb}GB for the image build) — free disk space first (docker system prune, or the df-watchdog disk auto-prune)"
    fi
    ok "disk preflight: $((_vib_free_kb / 1024 / 1024))GB free under $_vib_root (>= ${_vib_min_gb}GB)"
else
    ok "disk preflight: free space unknown (df unreadable) — continuing; the build fails loudly on a truly full disk"
fi
docker build -t waydroid-instance:latest "$CTX" \
    || fail "docker build failed (image tag left untouched)"

docker image inspect waydroid-instance:latest >/dev/null 2>&1 \
    || fail "waydroid-instance:latest missing after build"
# Verify EVERY file the Dockerfile COPYs is present and non-empty in the
# built image — not just the APKs. A stale Dockerfile (a helper COPY dropped
# by a bad merge) or a partial build context passes `docker build` but ships
# instances whose /opt helpers are missing; the entrypoint's legacy
# fallbacks would then silently engage on every new instance (no optimizer,
# no canonical keepalive) with zero build-time signal.
verify_chain=""
for pair in $COPY_PAIRS; do
    dest="${pair#*:}"
    verify_chain="${verify_chain:+$verify_chain && }test -s '$dest'"
done
# --entrypoint sh: never run the real entrypoint during verification.
docker run --rm --entrypoint sh waydroid-instance:latest -c "$verify_chain" \
    || fail "built image is missing files the Dockerfile COPYs into it"

ok "waydroid-instance:latest built with pinned Roblox APKs + all COPYd helpers (new instances pick it up)"
