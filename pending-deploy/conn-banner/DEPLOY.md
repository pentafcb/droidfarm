# Deploy: grid connectivity banner

The dashboard grid poll (`refresh()`, every 10s) used to fail silently —
a dropped tunnel or wedged manager left stale instance cards on screen
with zero indication. Now it counts consecutive refresh failures; after
2 of them a red "Sin respuesta del servidor — reintentando… (N)" banner
appears above the grid (N = consecutive failures). One successful poll
clears it. The cards keep their old data meanwhile (unchanged behavior).
401s skip the counter (the login flow already reloads the page there).

## What rides apply-all

No new stage files: `manager/static/index.html` is already in the
apply-all stage list, and this rides it from repo HEAD. Suite `t56` in
preflight: `pending-deploy/conn-banner/test_conn_banner.py` (29/29:
behavioral node run of the extracted block + dashboard contract).

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH (static file — no restart needed):
scp ~/workspace/droidfarm/manager/static/index.html \
    root@64.177.49.68:/root/jerusalem/static/index.html
```

## Verify

- Dashboard loads (HTTP 200); no banner visible while polls succeed.
- Kill the tunnel / stop jerusalem: within ~25s the red banner appears
  above the grid; restore the connection and it clears on the next poll.
- Served page contains `CONN_BANNER_AFTER` (served from disk per request).
