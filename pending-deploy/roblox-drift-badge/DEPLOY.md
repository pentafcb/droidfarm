# Deploy: Roblox version-drift dashboard badge

Closes the loop the v6 pin guard left open: drift was only a log line, and
Penta watches the dashboard, not logs. The watchdog now writes a
`<instance>.roblox_drift` marker (reason on first line); the manager surfaces
`roblox_drift` / `roblox_drift_reason` on `/api/instances`; the dashboard
shows an amber "Roblox actualizado — revisa la versión" badge with the
reason in the tooltip (priority: degraded > drift > setup states).

## What rides apply-all

No new stage files: `manager/app.py`, `manager/static/index.html`, and
`waydroid-image/df-watchdog.sh` are already in the apply-all stage list, and
the drift badge rides it from repo HEAD. Suite `t26` in preflight:
`pending-deploy/roblox-drift-badge/test_roblox_drift_badge.py` (30/30).

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH:
scp ~/workspace/droidfarm/manager/app.py \
    root@209.222.10.93:/root/jerusalem/app.py
scp ~/workspace/droidfarm/manager/static/index.html \
    root@209.222.10.93:/root/jerusalem/static/index.html
scp ~/workspace/droidfarm/waydroid-image/df-watchdog.sh \
    root@209.222.10.93:/usr/local/bin/df-watchdog.sh
ssh root@209.222.10.93 \
  'python3 -m py_compile /root/jerusalem/app.py && \
   bash -n /usr/local/bin/df-watchdog.sh && \
   systemctl restart jerusalem'
```

## Verify

- `systemctl is-active jerusalem` -> `active`
- `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/` -> `200`
- Dashboard card shows no drift badge on a pinned fleet (marker only appears
  on drift); force a check by writing a marker on the VPS and confirming the
  badge: `echo "test" > /tmp/df-watchdog/rbx1.roblox_drift`, refresh the
  dashboard (auth required), then `rm /tmp/df-watchdog/rbx1.roblox_drift`.

## Notes

- Drift detection is main-node only (df-watchdog.sh does not run on wing
  nodes); remote instances report `roblox_drift: false` until a wing-side
  guard is built.
- The badge never forces dashboard auto-refresh: drift needs a human
  decision (it auto-clears only when the client is back on the pinned build).
- The marker is independent of the circuit-breaker `.degraded` markers;
  section-6 auto-clear never touches it.
