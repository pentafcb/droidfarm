# waydroid-image-build — DEPLOY

Builds `waydroid-instance:latest` on the NEW VPS (64.177.49.68) with the
pinned Roblox 2.740.931 split APKs baked in, so new instances auto-install
Roblox at creation (entrypoint `pm install` of both splits).

## Why this exists

- Roblox dropped x86_64 from its newest builds; 2.740.931 is the newest
  x86_64 build and the farm's login tap coordinates are tuned to it.
- The only recovery copy is the 235MB XAPK at
  `~/workspace/your_files/droidfarm-apk-backup/roblox-2.740.931-x86_64.xapk`
  (verified 2026-10-12: sha256
  f50170541ee529f658ae79ca9f5b0cbf1c8544e1a49b6581185561c47759174e,
  contains com.roblox.client.apk + config.x86_64.apk with real
  lib/x86_64 native libs). The /tmp staging from the earlier session was
  wiped by a VM restart — do not stage APKs in /tmp again.
- The Dockerfile COPYs `roblox-base.apk` + `roblox-x86_64.apk`; without
  them in the build context the image build fails and new instances get
  no Roblox.

## Steps (run from an SSH-capable session)

Two ways to get the APKs onto the new VPS. Path A (curl on the VPS) is
the one Penta was already asked to do and needs no repo-machine upload
bandwidth; path B (extract + scp) is the fallback when the download
links are dead.

### Path A — curl the APKs directly on the VPS (preferred)

1. Copy the fetch script to the new VPS as root:
   ```
   scp pending-deploy/waydroid-image-build/fetch-roblox-apks.sh \
       root@64.177.49.68:/root/
   scp pending-deploy/waydroid-image-build/vps-image-build.sh \
       root@64.177.49.68:/root/
   ```

2. On the VPS as root — the two URLs are the Muse file-storage direct
   links (they EXPIRE 2026-10-15; a runonce `jerusalem-apk-expiry-check`
   fires 2026-10-14 17:30 PDT to nudge before expiry; if they are dead,
   use path B):
   ```
   bash /root/fetch-roblox-apks.sh '<base-apk-url>' '<x86_64-apk-url>'
   ```
   The script downloads both splits into `/root/jerusalem/waydroid-image/`
   (the docker build context) and verifies every byte against the pinned
   2.740.931 sha256 values. An expired link serves an HTML error page —
   the pin check fails closed on it ("bytes do not match ... link has
   likely EXPIRED"), stages nothing, and tells you to mint fresh links.
   Idempotent: re-running skips files that already match the pin.

3. Build (same script as path B):
   ```
   bash /root/vps-image-build.sh
   ```
   (dest defaulted to the build context, so no APK_SRC is needed.)

### Path B — extract locally + scp (fallback)

1. Extract the two splits on the repo machine (reads the backup, never
   modifies it):
   ```
   cd ~/workspace/droidfarm
   bash pending-deploy/waydroid-image-build/extract-roblox-apks.sh
   ```
   Expect: `extracted 2 APKs -> ./roblox-apks (base + x86_64 split, pinned 2.740.931)`
   with size + sha256 lines for both files.

2. Copy the APKs + the build script to the new VPS as root:
   ```
   scp roblox-apks/roblox-base.apk roblox-apks/roblox-x86_64.apk \
       root@64.177.49.68:/root/
   scp pending-deploy/waydroid-image-build/vps-image-build.sh \
       root@64.177.49.68:/root/
   ```

3. On the VPS as root (build context must be the repo's waydroid-image
   dir — scp it first if /root/jerusalem/waydroid-image is incomplete):
   ```
   APK_SRC=/root bash /root/vps-image-build.sh
   ```
   The script refuses to build when the context is incomplete, the
   Dockerfile COPY lines are missing, an APK is absent/empty, a staged
   APK's sha256 does not match the pinned 2.740.931 split (stale/swapped
   scp — the check names the file), an APK fails the arch gates (base
   must carry AndroidManifest.xml, split must carry `lib/x86_64/*.so`;
   needs `unzip` on the VPS — `apt-get install -y unzip` if the script
   fails closed on it), or Docker's data-root has less than 5GB free
   (fail-closed with the remediation; `VIB_MIN_FREE_GB` overrides the
   floor). The two SHA pins are hard-coded in both
   `fetch-roblox-apks.sh` and `vps-image-build.sh`; a pin move must update
   BOTH files (queue-audit check 10 fails the deploy on any disagreement).
   It never touches instance data, volumes, or running containers.

4. Verify (on the VPS) — the script already verifies every Dockerfile
   COPYd file inside the built image; this is the manual double-check:
   ```
   docker images waydroid-instance:latest
   docker run --rm --entrypoint sh waydroid-instance:latest \
       -c 'ls -la /opt/roblox-*.apk /opt/*.sh /entrypoint.sh'
   ```
   Both APKs plus all five helper scripts (android-optimize.sh,
   screencap-loop.sh, roblox-keepalive.sh, booted-watcher.sh,
   boot-complete.sh) and /entrypoint.sh must be listed and non-empty.

## After the build

- New instances created from the dashboard use the new image and get
  Roblox auto-installed at boot (existing containers are untouched).
- Creating the actual fleet (rbx1-20) on the new box is a MORNING
  DECISION for Penta: instance credentials (roblox_accounts.json) live on
  the old box's state dir, and recreating instances without them loses
  the account mapping. Do not mass-create until he confirms the account
  migration path.
- The old VPS (209.222.10.93) still holds the running fleet; its
  decommissioning is also Penta's call.
