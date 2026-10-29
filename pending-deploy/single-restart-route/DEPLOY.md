# DEPLOY.md — single-restart-route

## Rides apply-all (preferred)

No new stage files: `manager/app.py` + `manager/static/index.html` are
already in the apply-all stage list. Deploy = run
`pending-deploy/apply-all/deploy-all.sh` from an SSH-capable session
(preflight t141 runs this package's suite).

## Standalone (if apply-all is unavailable)

From the repo root, as root on the VPS (main node):

```bash
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html
ssh root@209.222.10.93 'systemctl restart jerusalem && sleep 2 && systemctl is-active jerusalem'
```

No restart is strictly needed for `index.html` alone (served from disk);
the restart is for `app.py`.

## Verify (after deploy)

1. `systemctl is-active jerusalem` → `active`.
2. Dashboard root returns HTTP 200.
3. A card shows a "Reiniciar" button next to Iniciar/Detener.
4. Authenticated `POST /api/instances/<name>/restart` no longer 404s
   (200 on a real instance; the card button exercises the same route).
5. `POST /api/instances/<name>/explode` still returns 404.

## Rollback

Re-deploy the previous `app.py` + `index.html` from the apply-all
backups (`/root/jerusalem-deploy/` timestamps) and restart jerusalem.
No state, data, or credentials are touched by this change.
