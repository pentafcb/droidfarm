# boot-install-timeout

Bounds the three foreground `waydroid shell` calls in
`waydroid-image/boot-complete.sh` (`pm list packages`, `pm install` of the
split APKs, `input keyevent 4`) with a `bcs()` timeout wrapper. A wedged
session used to hang the whole boot sequence after the `booted` marker was
written — dashboard "ready", no helpers, no Roblox, no recovery. Now a
timeout WARNs and the script continues, so helpers still start; the
dashboard shows "installing_roblox" honestly until a restart retries.

## Files

- `test_boot_install_timeout.sh` — 38/38 hermetic checks.
- `DEPLOY.md` — problem, fix, tests, image-build deploy steps.
- Product change: `waydroid-image/boot-complete.sh` (image-side; rides the
  next `vps-image-build.sh` via the Dockerfile COPY, not apply-all).

## Preflight

t98 — exact-count grep `38 passed, 0 failed`.
