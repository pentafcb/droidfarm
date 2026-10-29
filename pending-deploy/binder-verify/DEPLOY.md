# Deploy: binder-verify

## What it is
`verify-binder.sh` is a READ-ONLY binder stack verification for a fleet host.
It is repo-side (not staged to the VPS) and runs by hand once per check —
the morning SSH session's "binder post-reboot 64-device verification".

## Checks (exit 0 PASS / 1 FAIL / 2 usage)
1. `binder_linux` module visible in `/sys/module`
2. `/dev/binder` + `/dev/binder1`..`/dev/binder63` present — PASS floor 60
   (matches install.sh's warning threshold); 60–63 names the missing nodes
   as a warning
3. `/dev/binderfs/binder-control` present (the manager's instance-create
   binder self-heal needs it)
4. Persistent config so the pool survives reboot:
   - `/etc/modprobe.d/binder.conf` has `options binder_linux devices=` with
     exactly the 64-name list
   - FAILS CLOSED on the 2026-10-13 regression: a NUMERIC `devices=64`
     value (the module parameter is a comma-separated NAME list, not a
     count — a numeric value creates one device literally named "64" and
     zero /dev/binder* nodes)
   - `/etc/modules` loads `binder_linux` on boot

On FAIL it prints the exact remediation (regenerate the name list, write
`binder.conf`, add the `/etc/modules` line, reload the module or reboot).

## Run (from an SSH-capable session, as root on the target box)
```
scp pending-deploy/binder-verify/verify-binder.sh root@64.177.49.68:/tmp/
ssh root@64.177.49.68 'bash /tmp/verify-binder.sh'
```
Expected: `VERDICT: PASS` and `binder device nodes: 64/64`.

## Verify
- PASS → the box is ready for srt1–srt5 creation (manager's
  `ensure_binder_devices()` self-heals any shortfall at create time).
- FAIL → follow the printed remediation, then re-run. Do NOT create
  instances until PASS.

## Tests
`bash pending-deploy/binder-verify/test_verify_binder.sh` — 26/26 hermetic
checks (fake host root; nothing touches the real /dev or /etc). Wired as
apply-all preflight t57.
