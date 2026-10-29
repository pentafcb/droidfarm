# waydroid-image-build

One tested package for the pending Waydroid image build on the new VPS
(64.177.49.68): everything an SSH-capable session needs, nothing it has
to improvise.

## Contents

- `extract-roblox-apks.sh` — runs on the repo machine. Pulls
  `com.roblox.client.apk` → `roblox-base.apk` and `config.x86_64.apk` →
  `roblox-x86_64.apk` out of the 235MB XAPK backup, then verifies both
  are intact APKs (valid zip, base has AndroidManifest.xml, split has
  real `lib/x86_64/*.so` native libs). Fail-closed on missing entries,
  corrupt zips, or a mislabeled split. `ROBLOX_XAPK` / `APK_OUT` env
  overrides.
- `vps-image-build.sh` — runs ON the VPS as root. Preflights the build
  context (`/root/jerusalem/waydroid-image`) by DERIVING the required files
  from the Dockerfile's own single-source COPY lines (fail-closed on
  multi-source/glob/unsupported COPY forms) — entrypoint.sh plus every
  /opt helper the Dockerfile COPYs (android-optimize.sh,
  screencap-loop.sh, roblox-keepalive.sh, booted-watcher.sh,
  boot-complete.sh); the two APK COPY lines are checked for presence and
  the APKs themselves are staged from `APK_SRC`. BEFORE the build, both
  staged APKs are verified against the pinned 2.740.931 sha256 values
  (same pins as `fetch-roblox-apks.sh` — queue-audit check 10 fails the
  deploy if the two files' pins ever disagree) plus the arch gates the
  extract script uses (base carries AndroidManifest.xml, split carries
  real `lib/x86_64/*.so` libs — needs `unzip` on the VPS, fail-closed with
  an install hint). This closes the Path-B hole: scp'd splits used to skip
  both the fetch pin check and the extract arch check, so a stale or
  mislabeled file baked a broken Roblox into the fleet image. A free-space
  preflight then fails closed when Docker's data-root has < 5GB free
  (`VIB_MIN_FREE_GB`-overridable; warn-and-continue when `df` is
  unreadable) instead of dying mid-build with "no space left on device".
  Then runs
  `docker build -t waydroid-instance:latest` and verifies the tag plus
  EVERY COPYd destination is present and non-empty inside the built image
  (via `--entrypoint sh`, never running the real entrypoint) — not just
  the APKs. Idempotent; never touches instance data/volumes/containers.
  `IMAGE_CTX` / `APK_SRC` / `DF_PIN_*_SHA` (pin overrides — the escape
  hatch for a deliberate pin move after Penta approves it) /
  `DF_UNZIP_BIN` (test seam) env overrides.
- `fetch-roblox-apks.sh` — runs ON the VPS as root:
  `bash fetch-roblox-apks.sh <base-url> <x86_64-url> [dest-dir]`.
  Downloads both splits straight into the build context (default
  `/root/jerusalem/waydroid-image`) and verifies every byte against the
  pinned 2.740.931 sha256 values — fail-closed on expired-link HTML
  pages, curl errors, or zip-entry mismatches; idempotent (skips files
  that already match the pin). URLs are never printed (they may carry
  query-string tokens). `DF_PIN_*_SHA` overrides are TEST-ONLY.
- `test_waydroid_image_build.sh` — 62 hermetic checks (fake XAPKs built
  with real zips, fake docker/id on PATH): happy paths, exact entry
  selection (arm64 decoy must not leak), all fail-closed modes, rerun
  idempotency, derived-COPY-list proof (a new COPY line without its file
  fails closed — the preflight cannot go stale), multi-source/glob COPY
  fail-closed, post-build verify chain probing every COPYd destination,
  sha256 pin verification (tampered base/split fail closed naming the
  file and the pinned version, `DF_PIN_*_SHA` override honored), arch
  gates on the staged APKs (base without AndroidManifest.xml, split
  without x86_64 libs, arm64-only mislabeled split, non-zip base — all
  fail closed; missing `unzip` fails closed with an install hint),
  bash -n on both scripts.
- `test_fetch_roblox_apks.sh` — 29 hermetic checks (fake curl/id):
  usage, non-root/curl-missing refusal, happy path byte-identical
  staging, idempotent skip, corrupt-staged re-download, curl failure and
  expired-link HTML fail-closed with tmp cleanup, zip-entry sanity
  (missing manifest / missing x86_64 lib), no URL token in output, dest
  auto-create, default pins tied to the verified backup bytes,
  curl hardening flags, bash -n.
- `DEPLOY.md` — the exact SSH-session commands + verify steps, and the
  morning decisions left for Penta (fleet creation, old-box
  decommissioning).

## Wiring

Repo-side package (like queue-audit / live-verify): no staged files, so
it does not touch vps-apply.sh KEYS or the live-verify --ssh probe list.
It runs as apply-all preflight t49 (exact-count "76 passed, 0 failed");
the fetch script's suite runs as t55 (exact-count "29 passed, 0
failed").
