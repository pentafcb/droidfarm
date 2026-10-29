# post-deploy

One-command runner for every post-`apply-all` manual step on the main VPS.

**On the VPS as root, after `deploy-all.sh` succeeds:**

```bash
bash /root/jerusalem/finish-deploy.sh
```

It runs, in order (aborts on first failure; every step idempotent):

1. `push-keepalive.sh` — keepalive v2 into live containers
2. `push-booted-watcher.sh` — booted-watcher loop into live containers
3. `push-screencap.sh` — adaptive screencap loop into live containers
4. `repoint-backup-cron.sh` — 03:17 backup cron → `/root/jerusalem`
5. Cron-line switch — legacy backup cron line(s) → canonical backup-state.sh
   line, in root's personal crontab AND /etc/cron.d (no-op if already
   switched anywhere; fails closed if no backup cron exists anywhere)
6. `backup-state.sh` once + `verify-backup.sh` drill of the newest archive

Then run from the repo machine:

```bash
bash pending-deploy/live-verify/verify-live.sh --ssh
```

Files:

- `finish-deploy.sh` — the orchestrator (staged to `/root/jerusalem/`,
  run by hand; NOT executed by `vps-apply.sh`).
- `test_post_deploy.sh` — 84/84 hermetic tests.
- `DEPLOY.md` — problem/solution/tests/deploy notes.

Env overrides (tests): `FD_DF_DIR`, `FD_CRONTAB_BIN`, `FD_CROND_DIR`,
`FD_SKIP_ROOT_CHECK=1`, plus `JERUSALEM_*` passthrough to `backup-state.sh`.
