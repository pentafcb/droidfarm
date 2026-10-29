# login-script-cleanup — DEPLOY

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
3. Trigger a local Roblox login from a viewer Account panel, then on
   the VPS confirm no script lingers:
   `ls /var/lib/docker/volumes/wd-<name>-data/_data/data/media/0/jerusalem/auto_login.sh`
   -> No such file (the script exists only during the login exec).
4. On the next manager restart, a line like
   `[login] swept N stale login staging file(s)` appears only if a
   previous run crashed mid-login; normally no line at all.

Rollback: previous apply-all run dir under `/root/jerusalem-deploy/`
restores the prior `app.py`; `systemctl restart jerusalem`.
