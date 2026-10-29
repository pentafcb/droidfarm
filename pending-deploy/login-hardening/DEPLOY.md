# DEPLOY: login-hardening — dashboard sign-in robustness

## What
- `manager/app.py`: new `check_dashboard_password()` (constant-time, never
  raises). Fixes a real crash: `hmac.compare_digest(str, str)` raises
  `TypeError` for non-ASCII input, turning `/api/login` into an HTTP 500 —
  the old dashboard then showed "Wrong password." forever even with the
  right password. Non-string JSON input (null/number/list) also 401s
  cleanly instead of 500ing.
- `manager/static/index.html`: `saveToken`/`loadToken` with
  localStorage -> sessionStorage -> in-memory fallback (`localStorage.setItem`
  throws in iOS private mode / quota-exhausted); `logout` clears both stores;
  `doLogin` now distinguishes connection failure ("Connection failed — check
  your network and try again."), wrong password (401 -> "Wrong password."),
  and other server errors ("Server error (N) — try again in a moment.")
  instead of blaming every failure on the password.

## Tests
`python3 pending-deploy/login-hardening/test_login_hardening.py` — 33/33 pass.
Wired into apply-all preflight as suite t18.

## Deploy
Rides apply-all from HEAD: `manager/app.py` + `manager/static/index.html`
are already in the stage list (no new stage files). Or manually:

```
scp manager/app.py manager/static/index.html root@209.222.10.93:/root/jerusalem/
# (index.html served from disk — login JS takes effect on next page load)
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify
1. `curl -s -o /dev/null -w '%{http_code}\n' -X POST \
   -H 'Content-Type: application/json' -d '{"password":"nope"}' \
   http://127.0.0.1:8080/api/login` -> 401 (not 500).
2. POST with a non-ASCII password body -> 401, never 500.
3. Browser: wrong password -> "Wrong password."; stop the manager and sign
   in -> "Connection failed …" (proves errors are no longer conflated).
4. Sign in succeeds, token persists across reload; logout clears it.

## Notes
- Does NOT change the actual dashboard password or token scheme; tokens still
  live 24h and are stored server-side in tokens.json.
- The user's 2026-10-08 "password sign-in not working" report is unresolved
  pending their reply (which error they see). This change makes the next
  report diagnosable: a real 401 says "Wrong password.", a server/network
  problem now says so explicitly.
