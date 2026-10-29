# loop-device-guard — DEPLOY

## What it is
Closes the loop-device half of the fleet's kernel device story. Binder got
persistent provisioning + create-time self-heal + scripted verification;
loop devices (Waydroid needs ~2 per instance at boot — the 2026-10-08
recovery needed ~40 for 20) had only a one-shot `mknod` at install time that
does not survive a reboot on devtmpfs. A post-reboot host silently capped
the fleet at ~4 booting instances with no actionable error.

## Changes
- `manager/app.py` (rides apply-all from HEAD — already in the stage list):
  `count_loop_devices()` + `ensure_loop_devices()` (modprobe
  `loop max_loop=64` when the module isn't loaded, mknod for missing nodes,
  honest False when the live driver's max_loop is too small; never raises).
  `create_local_instance()` self-heals then fails OPEN with a loud
  `"warning"` on the 200 response (loop is consumed at boot, not reserved
  at create — a small fleet runs fine on the stock 8). `fleet_headroom()`
  (local branch) reports `loop_devices` + a plan-preview warning; the
  dashboard already renders `headroom.warning`, no dashboard change.
- `install.sh` step 4 (repo-side, fresh installs): `install_loop_pool()`
  writes `options loop max_loop=64` to `/etc/modprobe.d/loop.conf`, adds
  `loop` to `/etc/modules`, modprobes immediately, mknods missing nodes,
  verifies the count with a WARN below 60.
- `pending-deploy/binder-verify/verify-binder.sh` (repo-side, run by hand):
  new loop sections — module loaded, 64 block nodes (>=60 floor),
  max_loop capacity check, persistence via GRUB cmdline OR modprobe.d +
  modules line. The morning `verify-binder.sh` run now fails closed on a
  stock-8-loop host instead of passing and letting Create fleet hit the
  wall at instance ~5.

## Deploy
Full queue: `bash pending-deploy/apply-all/deploy-all.sh` (or
`bash pending-deploy/newbox-sync/sync-newbox.sh --yes` for 64.177.49.68),
then `bash /root/jerusalem/finish-deploy.sh` on the VPS.

Standalone (SSH session):
```
scp manager/app.py root@HOST:/root/jerusalem/manager/app.py
ssh root@HOST "python3 -m py_compile /root/jerusalem/manager/app.py && systemctl restart jerusalem"
```
Verify: `systemctl is-active jerusalem`; create a test instance on a healthy
host (no `warning` key); `fleet_headroom` plan on a stock-8 host shows the
loop warning; `bash verify-binder.sh` on the new VPS → `VERDICT: PASS`
after install.sh step 4.

## Tests
`python3 test_loop_device_guard.py` → 25/25; `bash test_loop_device_guard.sh`
→ 30/30. Preflight t66. Regressions re-verified: binder-verify 26/26,
create-preflight 22/22, fleet-create 71/71, batch-ops 51/51,
instance-resources 51/51, batch-journal 46/46, batch-resume 59/59,
batch-worker-safety 32/32.
