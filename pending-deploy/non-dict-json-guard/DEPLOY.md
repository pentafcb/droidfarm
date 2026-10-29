# DEPLOY: non-dict-json-guard

## What

One layer up from t148/t149, the same dropped-connection class lived in
the body parsers. `json.loads` accepts any JSON value, and both servers'
`_read_json` returned the parsed value verbatim — but every POST caller
does `data.get(...)`. A body of `["x"]`, `"username"`, `123`, `true`, or
`null` therefore raised `AttributeError`, the handler thread died
(`handle_error` only logs), and the connection dropped with NO response.
Worst case: `/api/login` is unauthenticated, so anyone could drop handler
threads with a one-line body; the register/install-progress routes died
BEFORE their join-token check (the `.get` runs first). The roblox-account
route had its own variant: a string body containing "username" passed
the `in` checks and then crashed on `body["username"]` (`TypeError`),
while a list body silently no-op'd with a misleading 200.

Now (same contract both sides): `_read_json` (manager + agent) coerces
any non-dict parsed value to `{}` — the single choke point covering all
12 manager callers (login, register, install-progress, heartbeat, batch,
fleet plan, fleet create, bulk accounts, create, input, webrtc offer) and
all 3 agent callers (create, input, resize), so each gets its normal clean
400/401 from validation instead of a dropped connection. The
roblox-account route additionally rejects a non-dict body with a clean
400 "invalid json" (the resize route already coerced via
`validate_resize_params`). Valid dict bodies pass through byte-identical.

Files changed: `manager/app.py` (`_read_json` + roblox-account guard),
`wing/agent.py` (1.0.44 -> 1.0.45, `_read_json`). No dashboard change; no
wire protocol change. All VERSION-tuple suites widened, exact pins moved
per the bump convention (a sweep double-replacement, `1.0.45, 1.0.45`,
was caught by the verification grep and repaired); remote-autologin
snapshots refreshed to HEAD.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py` and `wing/agent.py`; no new stage files. The wing
agent self-updates to 1.0.45 on the next heartbeat after the manager
deploy; the vultr node additionally needs Penta's installer re-run
only if its agent predates self-update (same re-run already needed
for earlier agent versions).

## Standalone (if apply-all is unavailable)

```bash
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp wing/agent.py root@209.222.10.93:/root/jerusalem/wing/agent.py
ssh root@209.222.10.93 'systemctl restart jerusalem && systemctl is-active jerusalem'
```

Verify: dashboard HTTP 200; `curl -X POST http://209.222.10.93:8080/api/login
-d '["x"]'` returns 401 (not a dropped connection); a valid login still
returns a token.
