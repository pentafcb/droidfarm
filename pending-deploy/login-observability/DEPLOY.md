# Deploy: Roblox login-result observability

## What it is
Every Roblox login attempt (auto-login watcher `by="auto"` + dashboard Login
button `by="manual"`) is now recorded in `roblox_login_results.json`
(outcome only — never credentials): `roblox_login()` wraps the old logic
(renamed to `_roblox_login_inner`) and records ok/reason/by/attempts.

New visibility:
- `/api/instances` cards carry `login_ok` / `login_ts` / `login_by`
  (local + remote branches) → dashboard card shows
  `✓ Roblox login OK` or `⚠ Roblox login failed`.
- `/api/roblox-accounts` adds per-account last-login fields + fleet-wide
  `logged_in` count → fleet bar shows "N/M have accounts · K logged in",
  Accounts modal lists per-instance results (✓ time / ⚠ reason).
- New GET `/api/instances/<name>/roblox-login-result` (auth-gated) → viewer
  Account panel shows the last attempt outcome.
- `roblox_login_results.json` gets 600 perms like `roblox_accounts.json`.

## Deploy
No new stage files — `manager/app.py` + `manager/static/index.html` are
already in the apply-all stage list. Rides apply-all from HEAD.

Standalone (if needed):
```
scp ~/workspace/droidfarm/manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp ~/workspace/droidfarm/manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify
- `systemctl is-active jerusalem` → active
- dashboard root → HTTP 200
- `/api/instances` cards include `login_ok`/`login_ts`/`login_by` keys
- `/api/roblox-accounts` includes `logged_in` (integer)
- browser: card shows login badge; fleet bar shows "…logged in"; Accounts
  modal lists last-login results; viewer Account panel shows last outcome.

## Tests
`python3 test_login_observability.py` → 44/44 (hermetic).
Wired into apply-all preflight as suite t12.
