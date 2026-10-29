# Deploy: fleet-resources-remote

No new stage files — `manager/app.py` is already in the apply-all stage
list and is deployed from repo HEAD. This package rides apply-all.

## Normal path

```bash
bash pending-deploy/apply-all/deploy-all.sh   # from an SSH-capable session
```

Preflight runs this package's suite as t136
(`test_fleet_resources_remote.py`, expect `PASS=54 FAIL=0`).

## Standalone (if apply-all is unavailable)

```bash
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```

## Verify (after restart, dashboard logged in)

1. `systemctl is-active jerusalem` → `active`.
2. GET `/api/instances` → `fleet_resources` present with the same 4 keys
   (`mem_used_mb`, `cpu_pct`, `instances_seen`, `host_mem_mb`).
3. With a wing node heartbeating: `instances_seen` counts local + remote
   reporting instances, and `host_mem_mb` exceeds main-node RAM by the
   wing node's RAM. With no wing nodes, numbers are identical to before.
4. Dashboard fleet bar ("RAM de flota …") renders as before.

## Rollback

Apply-all's automatic rollback restores the pre-deploy `app.py` backup.
Standalone: re-scp the previous `app.py` and restart `jerusalem`.
