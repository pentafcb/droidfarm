# Deploy: Nodes modal connectivity error line

The Nodes modal's list fetch (`refreshProviders()`, `/api/providers`) used
to swallow every failure with a bare `catch(e){}` — a dropped tunnel or
wedged manager left an empty/stale node list with zero indication. (The
grid got its own conn-banner; this modal had no equivalent.)

Now a failed fetch shows an honest red error line in the modal:
"Sin respuesta del servidor — no se pudieron cargar los nodos." A
successful load hides it again (textContent-only, Spanish, no emojis).
Re-opening the modal retries the fetch automatically.

The create / fleet-create provider dropdowns intentionally keep their
graceful degradation (on failure they still offer auto/local — no
misleading data, so no change there).

## What rides apply-all

No new stage files: `manager/static/index.html` is already in the
apply-all stage list, and this rides it from repo HEAD. Suite `t97` in
preflight: `pending-deploy/nodes-conn-err/test_nodes_conn_err.py`
(25/25: behavioral node run of the extracted block + dashboard contract).

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH (static file — no restart needed):
scp ~/workspace/droidfarm/manager/static/index.html \
    root@64.177.49.68:/root/jerusalem/static/index.html
```

## Verify

- Dashboard loads (HTTP 200); open Nodos — no error line while the
  fetch succeeds.
- Kill the tunnel / stop jerusalem: open Nodos — the red error line
  appears where the node list would be; restore the connection, re-open —
  the list loads and the error line is gone.
- Served page contains `provConnErr` (served from disk per request).
