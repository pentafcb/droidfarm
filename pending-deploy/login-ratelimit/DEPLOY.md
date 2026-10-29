# login-ratelimit — /api/login brute-force protection

## What it is

The dashboard password gate (`POST /api/login`) on the public VPS had NO rate
limiting — an attacker could try passwords indefinitely. (Provider registration
already had 10/min/IP; the login endpoint was missed.) This adds a sliding-window
limit: **10 failed logins per 60s per client IP → HTTP 429**. A successful login
resets that IP's counter. The dashboard's `doLogin()` shows a distinct
"Too many attempts — wait a minute and try again." message for 429 (it sits
before the 401 check — a 429 body has no token, so without the branch it would
have read as "Wrong password.").

Memory is bounded: the per-IP table is pruned of expired entries and hard-capped
at 1024 IPs (stalest-first eviction; eviction is fail-open for that IP only).
All helpers are best-effort and never raise — a rate-limiter bug can never take
down logins.

## Files changed (all ride apply-all from HEAD — no new stage files)

- `manager/app.py`: `LOGIN_ATTEMPTS` + `login_allowed()` / `record_login_failure()` /
  `clear_login_failures()` / `_prune_login_attempts()`; wired into `/api/login`.
- `manager/static/index.html`: 429 branch in `doLogin()`.

## Tests

`test_login_ratelimit.py` — 34/34 hermetic checks: helper boundary (9 ok / 10
blocked), window expiry, per-IP isolation, success-clears, cap eviction,
never-raises on garbage input, route ordering + 401/429 wiring, dashboard
429-before-401 contract.

## Deploy

No standalone deploy — rides `pending-deploy/apply-all/deploy-all.sh` from HEAD
(`app.py` + `index.html` are already in the stage list). Verify after deploy:
10 rapid wrong-password POSTs to `/api/login` → the 11th returns 429;
a correct login still returns 200 with a token.

## Rollback

Revert is the previous `app.py`/`index.html` via the apply-all automatic
rollback (or the `.deploy-backup` copies on the VPS).
