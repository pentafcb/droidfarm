# Deploy: boot-recovery-badge

No new stage files — `manager/app.py`, `manager/static/index.html`,
`waydroid-image/df-watchdog.sh`, and `wing/agent.py` are already in the
apply-all stage list and are deployed from repo HEAD. This package rides
apply-all.

## Normal path

```bash
bash pending-deploy/apply-all/deploy-all.sh   # from an SSH-capable session
```

Preflight runs this package's suite as t137
(`test_boot_recovery_badge.py`, expect `PASS=42 FAIL=0`).

## Standalone (if apply-all is unavailable)

```bash
scp manager/app.py manager/static/index.html root@209.222.10.93:/root/jerusalem/
scp waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh
scp wing/agent.py root@209.222.10.93:/root/jerusalem/agent.py
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py /root/jerusalem/agent.py && bash -n /usr/local/bin/df-watchdog.sh && systemctl restart jerusalem'
```

The wing node picks up the new agent.py on its next heartbeat
self-update (sha-verified, no installer re-run needed).

## Verify (after restart, dashboard logged in)

1. `systemctl is-active jerusalem` → `active`.
2. GET `/api/instances` → response carries `watchdog_global` (existing
   field); with no boot pause in progress it is `[]` alongside the
   other global entries when present.
3. Dashboard fleet bar area: no new banner while all green (the banner
   renders only when `watchdog_global` is non-empty).
4. Confirm the banner code path on the served page (a hand-touched
   marker is not a valid drill: on a live host `boot.done` already
   exists, so the watchdog's next run clears any foreign
   `boot.degraded` within a minute):
   `curl -s http://127.0.0.1:8080/static/index.html | grep -o "boot:'Arranque'"`.
   A real boot-pause drill needs a host reboot with a blocked binder
   pool and is deliberately not scripted here.

## Rollback

Apply-all's automatic rollback restores the pre-deploy backups.
Standalone: re-scp the previous files and restart `jerusalem`.
