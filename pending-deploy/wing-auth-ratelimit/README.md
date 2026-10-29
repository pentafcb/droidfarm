# wing-auth-ratelimit

Wing agent 1.0.18 -> 1.0.19: agent-key brute-force protection
(manager login-ratelimit parity).

## The gap

The manager got `/api/login` rate limiting (10 failed / 60s / IP -> 429)
plus a constant-time, never-raises password check. The wing agent's
internet-facing `X-Agent-Key` auth (ports 8090-8099) had neither: failed
attempts were unlimited and the key compare was a plain `==`. The ninth
"main had it, wing didn't" parity item in a row (1.0.11 session-death,
1.0.12 loop devices, 1.0.13 memory, 1.0.14 stuck-boot, 1.0.15 keepalive
mirror, 1.0.16 binderfs self-heal, 1.0.17 agent HTTP self-check, 1.0.18
create image preflight).

## Wing (`wing/agent.py`)

- `_auth_key_ok(received)`: constant-time compare via `hmac.compare_digest`.
  An empty/unset `AGENT_KEY` or non-string input refuses (fail closed);
  non-ASCII header input returns `False` instead of raising (the manager's
  `check_dashboard_password` contract).
- Sliding-window rate limit: `AUTH_RATE_MAX` (10) failed auths per
  `AUTH_RATE_WINDOW` (60s) per client IP -> HTTP 429
  `{"error": "too many attempts"}` — the same body as the manager's
  `/api/login` 429. A successful authed request clears that IP's counter.
  Table bounded at `AUTH_RATE_MAX_IPS` (1024, stalest-first eviction,
  fail-open for the evicted IP). All helpers best-effort, never raise.
- `Handler._check_auth()` -> `'health' | 'ok' | 'ratelimited' | 'denied'`:
  `/api/agent/health` stays unauthenticated by design (agent self-check +
  manager probes) and never counts; on authed routes the rate limit is
  checked BEFORE the key compare, so a correct key presented while
  rate-limited still gets 429 (same 429-before-credential ordering as the
  manager's `/api/login`). `do_GET` / `do_POST` / `do_DELETE` map
  `ratelimited` -> 429, `denied` -> 401; the old 401 body is unchanged.
- Zero manager/dashboard changes: the manager's `wing_request` already
  treats a 429 as a failed attempt (fail-fast `attempts=1` on the hot
  paths), and legitimate manager traffic always carries the right key.

## Tests

`test_wing_auth_ratelimit.py` — 48 checks, hermetic + one live localhost
server: VERSION exact bump + manager `>=1.0.2` gate, key-compare units
(correct/wrong/empty-key/non-str/non-ASCII, `hmac.compare_digest`
contract), rate-limit state machine (boundary, window expiry, per-IP
isolation, success-clears, cap eviction fail-open, never-raises, prune),
`_check_auth` wiring (health bypass, 10 failures -> ratelimited even with
the correct key, success-clears, query-string health keeps the old
raw-path semantics, never raises), route contract (429 in all three
methods, 429 before 401, exact 429 body, no legacy call sites), and a
live server round-trip (correct key -> 200, wrong -> 401, 10 wrong -> 429
with the exact body, health -> 200 unauthenticated and uncounted).
