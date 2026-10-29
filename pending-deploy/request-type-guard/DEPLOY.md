# DEPLOY: request-type-guard

## What

One layer sideways from t148/t149/t150: the same dropped-connection
class lived in two request values used as an iterable / dict key
without a type check.

1. POST /api/instances/batch `{names: 5}` — `batch_create_job` did
   `[str(n) for n in names]` with no isinstance check, so a non-list
   `names` raised TypeError on iteration (int/bool) or silently
   iterated dict keys / string chars, killing the handler thread
   with no response.
2. POST /api/providers/heartbeat `{provider_id: ["x"]}` — the handler
   did `providers.get(pid)` before the agent-key check, so an
   unhashable provider_id raised TypeError and dropped the
   connection before auth ever ran. The same line also crashed on a
   corrupt providers file (a JSON list) and on a non-dict provider
   record (`p["agent_key"]` on a string), and an empty stored
   agent_key authenticated against an empty supplied key because
   hmac.compare_digest("", "") is True.

Now (manager/app.py only, no agent change, no dashboard change, no
wire protocol change): `batch_create_job` returns a clean 400 "names
must be a list" for any truthy non-list `names` (falsy still means
"all instances"); the heartbeat handler coerces a non-dict providers
mapping to {}, a non-string provider_id to "", and fails closed with
403 on a non-dict record, a missing key, or an empty stored key.
Valid inputs behave identically (same 202/200 codes, same job and
heartbeat shapes). The wing agent is untouched (still 1.0.45), so no
VERSION bump and no agent self-update rides this package.
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

Verify: dashboard HTTP 200; `curl -X POST
http://209.222.10.93:8080/api/instances/batch -H "Authorization: Bearer
<token>" -d '{"action": "stop", "names": 5}'` returns 400 "names
must be a list" (not a dropped connection); a valid batch still
returns 202; a heartbeat with a list provider_id returns 403.
