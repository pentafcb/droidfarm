# DEPLOY: state-shape-guard

## What

One layer sideways from t148/t149/t150/t151: the same dropped-connection
class lived in the manager's JSON state loaders, which returned
`json.load()` verbatim. A state file holding a valid JSON value of the
wrong shape (hand edit, disk corruption — the atomic tmp+rename saves
make a truncated file near-impossible, but a JSON list/string is still
parseable) crashed every caller instead of behaving like a missing file:

1. `load_providers` / `load_instances` / `load_accounts` on a list,
   string, number, or null file returned that value; every caller doing
   `.items()` / `.get()` then raised AttributeError, killing the
   handler thread with no response. Only `load_login_results` already
   guarded (isinstance dict else {}), and only a few call sites guarded
   — the other ~70 call sites did not, including the dashboard's
   `/api/instances` poll and the public `/api/providers/register`
   route (which does `providers.items()`).
2. `load_tokens` did `json.load(f).items()` inline: a list file raised
   AttributeError, and a dict with a non-numeric expiry raised
   TypeError on `e > time.time()`. `main()` calls `load_tokens()`
   unguarded, so either shape killed the manager AT STARTUP into a
   systemd restart loop — a total dashboard outage, where a missing
   tokens file simply means "no sessions".

Now (manager/app.py only, no agent change, no dashboard change, no wire
protocol change): the three loaders coerce any non-dict parsed value to
{} (the `load_login_results` idiom — non-dict means "no state", exactly
like a missing file); `load_tokens` coerces a non-dict file to {} and
drops malformed entries individually, so valid sessions survive a
single bad expiry. Valid dict files load exactly as before.
The wing agent is untouched (still 1.0.45), so no VERSION bump and no
agent self-update rides this package.
Remote-autologin app.py snapshot refreshed to HEAD.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py`; no new stage files.

## Standalone (if apply-all is unavailable)

```bash
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'systemctl restart jerusalem && systemctl is-active jerusalem'
```

Verify: dashboard HTTP 200; with a corrupt (JSON-list) providers file
on a test host, `/api/providers` returns 200 with an empty list instead
of a dropped connection, and a tokens file holding a list no longer
prevents the manager from starting (sessions simply start empty).
