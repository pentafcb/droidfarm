#!/usr/bin/env bash
# test_waydroid_image_build.sh — hermetic tests for extract-roblox-apks.sh
# and vps-image-build.sh. No network, no docker, no root required.
set -u
PKG="$(cd "$(dirname "$0")" && pwd)"
PASS=0; FAILN=0
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

pass() { PASS=$((PASS+1)); }
fail() { FAILN=$((FAILN+1)); echo "FAIL: $1"; }
run() { # run <desc> <expected_exit> -- <cmd...>
    local desc="$1" want="$2"; shift 2
    [ "$1" = "--" ] && shift
    local out rc
    out=$("$@" 2>&1); rc=$?
    if [ "$rc" = "$want" ]; then pass; else fail "$desc (want exit $want, got $rc): $out"; fi
}

make_xapk() { # make_xapk <path> [variant] — real zip via python3
    python3 - "$1" "${2:-good}" <<'EOF'
import sys, zipfile, io
path, variant = sys.argv[1], sys.argv[2]
def nested(entries):
    b = io.BytesIO(); zz = zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED)
    for n, c in entries: zz.writestr(n, c)
    zz.close(); return b.getvalue()
z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
if variant != "no-base":
    base_entries = [("AndroidManifest.xml", b"<manifest/>"), ("classes.dex", b"dex")]
    if variant == "base-no-manifest":
        base_entries = [("classes.dex", b"dex")]
    z.writestr("com.roblox.client.apk", nested(base_entries))
if variant != "no-split":
    split_entries = [("lib/x86_64/libx.so", b"elf"), ("assets/f.txt", b"x")]
    if variant == "split-no-lib":
        split_entries = [("assets/f.txt", b"x")]
    z.writestr("config.x86_64.apk", nested(split_entries))
z.writestr("config.arm64_v8a.apk", nested([("lib/arm64-v8a/libx.so", b"elf")]))  # decoy
z.writestr("manifest.json", b"{}")
z.close()
EOF
}

EX="$PKG/extract-roblox-apks.sh"
VB="$PKG/vps-image-build.sh"

# ---- extract: happy path ----
make_xapk "$T/good.xapk"
run "extract happy path" 0 -- env ROBLOX_XAPK="$T/good.xapk" APK_OUT="$T/out" bash "$EX"
[ -f "$T/out/roblox-base.apk" ] && [ -f "$T/out/roblox-x86_64.apk" ] && pass || fail "extract outputs exist"
[ -s "$T/out/roblox-base.apk" ] && [ -s "$T/out/roblox-x86_64.apk" ] && pass || fail "extract outputs non-empty"
env ROBLOX_XAPK="$T/good.xapk" APK_OUT="$T/out2" bash "$EX" 2>&1 | grep -q "sha256=" && pass || fail "extract logs sha256"
env ROBLOX_XAPK="$T/good.xapk" APK_OUT="$T/out2" bash "$EX" 2>&1 | grep -q "pinned 2.740.931" && pass || fail "extract final ok line"
# exact entry selection: arm64 decoy must NOT leak into the x86_64 output
unzip -Z1 "$T/out/roblox-x86_64.apk" | grep -q "arm64" && fail "arm64 decoy leaked into x86_64 apk" || pass

# ---- extract: failure modes ----
run "missing XAPK" 1 -- env ROBLOX_XAPK="$T/nope.xapk" APK_OUT="$T/o1" bash "$EX"
env ROBLOX_XAPK="$T/nope.xapk" APK_OUT="$T/o1" bash "$EX" 2>&1 | grep -q "XAPK not found" && pass || fail "missing XAPK names the file"
make_xapk "$T/nosplit.xapk" no-split
run "XAPK missing split entry" 1 -- env ROBLOX_XAPK="$T/nosplit.xapk" APK_OUT="$T/o2" bash "$EX"
env ROBLOX_XAPK="$T/nosplit.xapk" APK_OUT="$T/o2" bash "$EX" 2>&1 | grep -q "config.x86_64.apk" && pass || fail "missing split names the entry"
make_xapk "$T/nobase.xapk" no-base
run "XAPK missing base entry" 1 -- env ROBLOX_XAPK="$T/nobase.xapk" APK_OUT="$T/o3" bash "$EX"
make_xapk "$T/nolib.xapk" split-no-lib
run "split without x86_64 lib" 1 -- env ROBLOX_XAPK="$T/nolib.xapk" APK_OUT="$T/o4" bash "$EX"
env ROBLOX_XAPK="$T/nolib.xapk" APK_OUT="$T/o4" bash "$EX" 2>&1 | grep -q "x86_64" && pass || fail "no-lib failure mentions x86_64"
make_xapk "$T/nomanifest.xapk" base-no-manifest
run "base without manifest" 1 -- env ROBLOX_XAPK="$T/nomanifest.xapk" APK_OUT="$T/o5" bash "$EX"
echo "garbage-not-a-zip" > "$T/corrupt.xapk"
run "corrupt XAPK" 1 -- env ROBLOX_XAPK="$T/corrupt.xapk" APK_OUT="$T/o6" bash "$EX"
touch "$T/afile"
run "output path is a file" 1 -- env ROBLOX_XAPK="$T/good.xapk" APK_OUT="$T/afile" bash "$EX"

# ---- vps-image-build: fakes ----
BIN="$T/bin"; mkdir -p "$BIN"
# fake docker: DOCKER_FAIL=<subcommand> forces failure.
# The `run --rm` case logs its argv so tests can assert the post-build
# verify chain actually probes every COPYd file (DOCKER_ARGS_LOG).
cat > "$BIN/docker" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "build -t") [ "${DOCKER_FAIL:-}" = "build" ] && exit 1; exit 0 ;;
  "image inspect") [ "${DOCKER_FAIL:-}" = "inspect" ] && exit 1; exit 0 ;;
  "info --format") [ -n "${DOCKER_INFO_ROOT:-}" ] && echo "$DOCKER_INFO_ROOT"; exit 0 ;;
  "run --rm")
    [ -n "${DOCKER_ARGS_LOG:-}" ] && printf '%s\n' "$@" >> "$DOCKER_ARGS_LOG"
    [ "${DOCKER_FAIL:-}" = "run" ] && exit 1
    exit 0 ;;
esac
exit 0
EOF
chmod +x "$BIN/docker"
cat > "$BIN/id" <<'EOF'
#!/usr/bin/env bash
[ "${FAKE_UID:-0}" = "0" ] && echo 0 || echo "${FAKE_UID}"
EOF
chmod +x "$BIN/id"
cat > "$BIN/df" <<'EOF'
#!/usr/bin/env bash
# fake df: DF_AVAIL_KB sets the KiB reported on the Avail line;
# DF_FAIL=1 makes df fail (unreadable).
[ "${DF_FAIL:-}" = "1" ] && exit 1
echo "Avail"
echo "${DF_AVAIL_KB:-999999999}"
EOF
chmod +x "$BIN/df"

# The good fixture mirrors the real waydroid-image/Dockerfile COPY set
# (entrypoint.sh + the five /opt helpers + the two APKs). If the real
# Dockerfile gains another helper, add it to HELPERS here — the product
# derives its preflight from the Dockerfile, so the fixture must stay in
# sync to prove the derivation works.
HELPERS="entrypoint.sh android-optimize.sh screencap-loop.sh roblox-keepalive.sh booted-watcher.sh boot-complete.sh"
make_ctx() { # make_ctx <dir> [variant]
    local d="$1" v="${2:-good}" f dest
    mkdir -p "$d"
    echo "FROM ubuntu:22.04" > "$d/Dockerfile"
    for f in $HELPERS; do
        dest="/opt/$f"; [ "$f" = "entrypoint.sh" ] && dest="/entrypoint.sh"
        echo "COPY $f $dest" >> "$d/Dockerfile"
    done
    # extra-helper: a NEW Dockerfile COPY line with no file in the context —
    # proves the preflight derives from the Dockerfile, not a hard-coded list
    [ "$v" = "extra-helper" ] && echo "COPY extra-helper.sh /opt/extra-helper.sh" >> "$d/Dockerfile"
    [ "$v" = "multi-copy" ] && echo "COPY a.sh b.sh /opt/" >> "$d/Dockerfile"
    [ "$v" = "glob-copy" ] && echo "COPY *.sh /opt/" >> "$d/Dockerfile"
    [ "$v" != "no-copy-base" ] && echo "COPY roblox-base.apk /opt/roblox-base.apk" >> "$d/Dockerfile"
    [ "$v" != "no-copy-split" ] && echo "COPY roblox-x86_64.apk /opt/roblox-x86_64.apk" >> "$d/Dockerfile"
    for f in $HELPERS; do
        [ "$v" = "no-helper" ] && [ "$f" = "booted-watcher.sh" ] && continue
        echo "# $f" > "$d/$f"
    done
}
make_src() { # make_src <dir> [variant] — real APK zips via python3
    python3 - "$1" "${2:-good}" <<'EOF'
import sys, os
d, variant = sys.argv[1], sys.argv[2]
os.makedirs(d, exist_ok=True)
def apk(path, entries):
    import zipfile
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    for n, c in entries:
        z.writestr(n, c)
    z.close()
base = [("AndroidManifest.xml", b"<manifest/>"), ("classes.dex", b"dex")]
split = [("lib/x86_64/libx.so", b"elf"), ("assets/f.txt", b"x")]
if variant == "base-no-manifest":
    base = [("classes.dex", b"dex")]
elif variant == "split-no-lib":
    split = [("assets/f.txt", b"x")]
elif variant == "split-arm64-only":
    split = [("lib/arm64-v8a/libx.so", b"elf")]
if variant == "base-garbage":
    open(d + "/roblox-base.apk", "wb").write(b"not-a-zip")
else:
    apk(d + "/roblox-base.apk", base)
apk(d + "/roblox-x86_64.apk", split)
EOF
}
pin_src() { # pin_src <src-dir>: export DF_PIN_*_SHA for that dir's APKs
    export DF_PIN_BASE_SHA="$(sha256sum "$1/roblox-base.apk" | cut -d' ' -f1)"
    export DF_PIN_X86_SHA="$(sha256sum "$1/roblox-x86_64.apk" | cut -d' ' -f1)"
}

make_ctx "$T/ctx"; make_src "$T/src"; pin_src "$T/src"
export DOCKER_ARGS_LOG="$T/docker-run-args.log"
run "vps build happy path" 0 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
[ -f "$T/ctx/roblox-base.apk" ] && [ -f "$T/ctx/roblox-x86_64.apk" ] && pass || fail "APKs copied into context"
env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "waydroid-instance:latest built" && pass || fail "vps build final ok line"
# the post-build verify must probe EVERY COPYd file, not just the APKs
for vf in /entrypoint.sh /opt/android-optimize.sh /opt/screencap-loop.sh /opt/roblox-keepalive.sh /opt/booted-watcher.sh /opt/boot-complete.sh /opt/roblox-base.apk /opt/roblox-x86_64.apk; do
    grep -q "test -s '$vf'" "$DOCKER_ARGS_LOG" && pass || fail "verify chain probes $vf"
done
run "vps build idempotent rerun" 0 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"

run "non-root refused" 1 -- env PATH="$BIN:/usr/bin:/bin" FAKE_UID=1000 IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
run "missing context dir" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/noctx" APK_SRC="$T/src" bash "$VB"
mkdir -p "$T/ctx-nodocker"; echo x > "$T/ctx-nodocker/Dockerfile"
run "context without Dockerfile" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-nodocker" APK_SRC="$T/src" bash "$VB"
make_ctx "$T/ctx-nohelper" no-helper
run "context missing helper script" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-nohelper" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-nohelper" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "booted-watcher.sh" && pass || fail "missing helper named"
# derived-list proof: a Dockerfile COPY line for a file the hard-coded list
# never knew about must still fail closed, naming the file
make_ctx "$T/ctx-extra" extra-helper
run "new COPY line without file fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-extra" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-extra" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "extra-helper.sh" && pass || fail "new COPY file named"
make_ctx "$T/ctx-multi" multi-copy
run "multi-source COPY fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-multi" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-multi" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "unsupported COPY form" && pass || fail "multi-source names the form"
make_ctx "$T/ctx-glob" glob-copy
run "glob COPY fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-glob" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-glob" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "not a plain filename" && pass || fail "glob names the source"
make_ctx "$T/ctx-nocopy" no-copy-base
run "Dockerfile missing COPY base" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-nocopy" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx-nocopy" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "no COPY roblox-base.apk" && pass || fail "missing COPY line named"
mkdir -p "$T/src-noapk"; echo x > "$T/src-noapk/roblox-base.apk"
run "missing split APK in src" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-noapk" bash "$VB"
mkdir -p "$T/src-empty"; echo x > "$T/src-empty/roblox-base.apk"; : > "$T/src-empty/roblox-x86_64.apk"
run "empty APK refused" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-empty" bash "$VB"
run "docker build failure" 1 -- env PATH="$BIN:/usr/bin:/bin" DOCKER_FAIL=build IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
run "docker missing from PATH" 1 -- env PATH="/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
run "image verify run fails" 1 -- env PATH="$BIN:/usr/bin:/bin" DOCKER_FAIL=run IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"

# ---- vps-image-build: sha256 pin + arch verification ----
# A tampered base (bytes appended) no longer matches the exported pin.
mkdir -p "$T/src-badbase"
cp "$T/src/roblox-base.apk" "$T/src-badbase/"; cp "$T/src/roblox-x86_64.apk" "$T/src-badbase/"
echo "tampered" >> "$T/src-badbase/roblox-base.apk"
run "base sha mismatch fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-badbase" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-badbase" bash "$VB" 2>&1 | grep -q "roblox-base.apk sha256 mismatch" && pass || fail "mismatch names roblox-base.apk"
mkdir -p "$T/src-badsplit"
cp "$T/src/roblox-base.apk" "$T/src-badsplit/"; cp "$T/src/roblox-x86_64.apk" "$T/src-badsplit/"
echo "tampered" >> "$T/src-badsplit/roblox-x86_64.apk"
run "split sha mismatch fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-badsplit" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-badsplit" bash "$VB" 2>&1 | grep -q "roblox-x86_64.apk sha256 mismatch" && pass || fail "mismatch names roblox-x86_64.apk"
env PATH="$BIN:/usr/bin:/bin" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-badsplit" bash "$VB" 2>&1 | grep -q "2.740.931" && pass || fail "mismatch names the pinned version"
# The DF_PIN_*_SHA override is actually consulted (bogus override -> mismatch
# even against the good fixtures).
run "DF_PIN override is honored" 1 -- env PATH="$BIN:/usr/bin:/bin" DF_PIN_BASE_SHA="0000000000000000000000000000000000000000000000000000000000000000" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
# Arch fixtures are re-pinned to their own sha so the failure under test is
# the arch gate, not the sha gate.
make_src "$T/src-nomanifest" base-no-manifest
run "base without AndroidManifest.xml fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" DF_PIN_BASE_SHA="$(sha256sum "$T/src-nomanifest/roblox-base.apk" | cut -d' ' -f1)" DF_PIN_X86_SHA="$(sha256sum "$T/src-nomanifest/roblox-x86_64.apk" | cut -d' ' -f1)" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-nomanifest" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" DF_PIN_BASE_SHA="$(sha256sum "$T/src-nomanifest/roblox-base.apk" | cut -d' ' -f1)" DF_PIN_X86_SHA="$(sha256sum "$T/src-nomanifest/roblox-x86_64.apk" | cut -d' ' -f1)" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-nomanifest" bash "$VB" 2>&1 | grep -q "AndroidManifest" && pass || fail "no-manifest names AndroidManifest"
make_src "$T/src-nolib" split-no-lib
run "split without x86_64 libs fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" DF_PIN_BASE_SHA="$(sha256sum "$T/src-nolib/roblox-base.apk" | cut -d' ' -f1)" DF_PIN_X86_SHA="$(sha256sum "$T/src-nolib/roblox-x86_64.apk" | cut -d' ' -f1)" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-nolib" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" DF_PIN_BASE_SHA="$(sha256sum "$T/src-nolib/roblox-base.apk" | cut -d' ' -f1)" DF_PIN_X86_SHA="$(sha256sum "$T/src-nolib/roblox-x86_64.apk" | cut -d' ' -f1)" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-nolib" bash "$VB" 2>&1 | grep -q "x86_64" && pass || fail "no-lib names x86_64"
# A mislabeled arm64-only split must not pass as the x86_64 split.
make_src "$T/src-arm64" split-arm64-only
run "arm64-only split fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" DF_PIN_BASE_SHA="$(sha256sum "$T/src-arm64/roblox-base.apk" | cut -d' ' -f1)" DF_PIN_X86_SHA="$(sha256sum "$T/src-arm64/roblox-x86_64.apk" | cut -d' ' -f1)" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-arm64" bash "$VB"
make_src "$T/src-garbage" base-garbage
run "base not a zip fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" DF_PIN_BASE_SHA="$(sha256sum "$T/src-garbage/roblox-base.apk" | cut -d' ' -f1)" DF_PIN_X86_SHA="$(sha256sum "$T/src-garbage/roblox-x86_64.apk" | cut -d' ' -f1)" IMAGE_CTX="$T/ctx" APK_SRC="$T/src-garbage" bash "$VB"
run "unzip missing fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" DF_UNZIP_BIN="no-such-unzip-tool" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" DF_UNZIP_BIN="no-such-unzip-tool" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "unzip" && pass || fail "unzip-missing names unzip"

# ---- vps-image-build: disk-space preflight ----
# DF_AVAIL_KB is KiB; the 5GB floor is 5242880 KiB.
mkdir -p "$T/droot" "$T/droot-info"
run "disk low fails closed" 1 -- env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=1048576 VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=1048576 VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "free disk space" && pass || fail "low-disk names the fix"
env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=1048576 VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "need >= 5GB" && pass || fail "low-disk names the floor"
run "disk at floor passes" 0 -- env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=5242880 VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
run "disk above floor passes" 0 -- env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=10485760 VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=10485760 VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "disk preflight: 10GB free under $T/droot" && pass || fail "ok line names dir and GB"
run "df failure warns and continues" 0 -- env PATH="$BIN:/usr/bin:/bin" DF_FAIL=1 VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" DF_FAIL=1 VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "free space unknown" && pass || fail "df-fail says unknown"
run "garbage floor sanitized to 5" 1 -- env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=3145728 VIB_MIN_FREE_GB="lots" VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=3145728 VIB_MIN_FREE_GB="lots" VIB_DOCKER_ROOT="$T/droot" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "need >= 5GB" && pass || fail "garbage floor falls back to 5GB"
run "docker info root honored" 0 -- env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=10485760 DOCKER_INFO_ROOT="$T/droot-info" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=10485760 DOCKER_INFO_ROOT="$T/droot-info" IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "under $T/droot-info" && pass || fail "docker-info root used"
run "empty docker info falls back" 0 -- env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=10485760 IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB"
env PATH="$BIN:/usr/bin:/bin" DF_AVAIL_KB=10485760 IMAGE_CTX="$T/ctx" APK_SRC="$T/src" bash "$VB" 2>&1 | grep -q "disk preflight:" && pass || fail "fallback path logs the preflight"

bash -n "$EX" && pass || fail "bash -n extract-roblox-apks.sh"
bash -n "$VB" && pass || fail "bash -n vps-image-build.sh"

echo "$PASS passed, $FAILN failed"
[ "$FAILN" = "0" ]
