# auto-login-success — DEPLOY

Rides apply-all from HEAD. No new stage files: `manager/app.py` is
already in the stage list.

Deploy (from an SSH-capable session):

```
bash pending-deploy/apply-all/deploy-all.sh
```

or the standalone equivalent:

```
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```

Verify after deploy:

1. `systemctl is-active jerusalem` -> `active`.
2. Dashboard login works (HTTP 200 on `/`).
3. On an instance with auto-login enabled, after its next first boot,
   the dashboard card shows "Roblox login OK" once — and the manager
   log shows exactly ONE `[auto-login] <name>` line for that boot
   (before this fix it showed three, one per 60s watcher pass).
4. A failed login still retries (up to 3 attempts) — check
   `roblox_login_results.json` for the instance after a bad-password
   login: attempts climb 1, 2, 3, then the boot is marked done.

Rollback: previous apply-all run dir under `/root/jerusalem-deploy/`
restores the prior `app.py`; `systemctl restart jerusalem`.
