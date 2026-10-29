# boot-complete

Late-boot setup for Waydroid instances: guarantees the full boot-completion
sequence (Roblox install, helper scripts, helpers, optimizer, launch) runs
even when Android takes longer than the entrypoint's 5-minute boot window.

## Files

- `test_boot_complete.sh` — 63/63 hermetic checks (see DEPLOY.md for coverage)
- `DEPLOY.md` — problem, fix, tests, deploy, verify

## Product changes (all in `waydroid-image/`)

- `boot-complete.sh` (new) — the canonical boot-completion sequence
- `entrypoint.sh` — fast path delegates to `/opt/boot-complete.sh`
- `Dockerfile` — COPYs it to `/opt`
- `booted-watcher.sh` — late-boot hook runs it when the marker was missing
- `df-watchdog.sh` — sections 3+4: distinct per-day WARN + no budget burn
  when a helper script is missing from the data volume

## Preflight wiring

- `deploy-all.sh` t87 (`63 passed, 0 failed`), `bash -n` gate, header +
  green-log + suite lists (87 suites)
- `android-optimizer` test 44→45, `adaptive-screencap` test 42→43
  (assertions moved from `entrypoint.sh` to `boot-complete.sh`)
