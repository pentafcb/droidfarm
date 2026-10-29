# DEPLOY: instance health surfacing (watchdog DEGRADED -> dashboard badge)

Built + tested 2026-10-07 ~09:40 UTC. 17/17 checks pass
(`test_instance_health.py`); no regressions (h264 59/59, remote-autologin 32/32,
watchdog-v4 26/26, py_compile + bash -n clean).

## What it does
The watchdog's circuit breaker used to mark a wedged instance DEGRADED in
`/tmp/df-watchdog/<name>.degraded` and pause auto-recovery — but the dashboard
kept showing a green "Ready" card, so nobody knew an instance needed attention.
Now the reason flows end-to-end:

1. `waydroid-image/df-watchdog.sh` (v4.1): `cb_guard` writes the human-readable
   reason as the first line of the `.degraded` marker (was an empty file).
2. `manager/app.py`: new `instance_health(name)` reads the marker (best-effort,
   never raises, name sanitized); `/api/instances` emits `degraded` +
   `degraded_reason` for local instances (`false`/`null` for remote nodes —
   the watchdog only runs on the main node).
3. `manager/static/index.html`: red "Needs attention — auto-recovery paused"
   badge (reason in tooltip) takes priority over the setup badge; the card
   list keeps auto-refreshing while any instance is degraded (degraded
   auto-clears when the watchdog sees all green again).

## Deploy
No new deploy package needed — all three files are already in the apply-all
stage list, deployed from repo HEAD. Just run (from a session WITH VPS SSH):

```bash
bash pending-deploy/apply-all/deploy-all.sh
```

## Verify (after deploy)
```bash
# manager exposes the fields (unauthenticated probe of the auth gate only)
curl -s http://127.0.0.1:8080/api/instances | head -c 100   # expect 401
# functional check: fake a degraded marker for a nonexistent instance name,
# then confirm the API reports it (authed), then remove the marker
DF=/tmp/df-watchdog
echo "container restarted 7 times in last 3600s - auto-recovery paused" > $DF/df-probe.degraded
# ... GET /api/instances with dashboard token -> df-probe shows degraded:true
rm -f $DF/df-probe.degraded
```
Dashboard: open an instance card while its marker exists -> red badge with the
reason as tooltip. Delete the marker -> badge clears on next refresh.

## Notes
- Marker dir defaults to `/tmp/df-watchdog` on both sides (override via `DF_ST`).
- Degraded markers vanish on reboot (`/tmp`) — acceptable: fresh boot = fresh budget.
- No state, credentials, or instance data touched. No secrets in the diff.
