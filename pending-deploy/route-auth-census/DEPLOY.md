# route-auth-census (t157) — DEPLOY

## What it is

Repo-side preflight gate only — nothing to deploy to the VPS, no stage
files, no service restart. It runs from the repo during
`pending-deploy/apply-all/deploy-all.sh` preflight, like the JS gate and
the queue-audit.

## What it checks

`test_route_auth_census.py` (78 checks, hermetic, no network) extracts
every HTTP route branch from the manager (`manager/app.py`) and wing
agent (`wing/agent.py`) Handler methods and proves each one is
authenticated:

- Manager `do_GET`: every branch carries its own `_authed()` check, or
  is classified: `/` + `/static/` (public dashboard page, served only
  via `_serve_static`), `/api/agent/update/download` (agent-key),
  `/api/install/` (join-token), `/ws/screen/` + `/ws/h264/` (handlers
  check `_authed_ws()`).
- Manager `do_POST`: the global gate precedes every authed route; the
  only pre-gate routes are the four public ones, each with its own auth
  evidence (`/api/login` ratelimit + password, `/api/providers/register`
  join-token, install-progress join-token, heartbeat agent-key).
- Manager `do_DELETE`: gate precedes every route branch.
- Wing agent `do_GET`/`do_POST`/`do_DELETE`: `_check_auth()` precedes
  every route branch; `/api/agent/health` is the only public route, by
  design, and returns no credentials or instance data.
- Fail-closed extras: no `do_PUT`/`do_PATCH`/`do_HEAD`/`do_OPTIONS`
  handler may exist unclassified, and the extracted route set must equal
  the expected set exactly — a new, renamed, or moved route fails the
  suite until it is classified here.

## Verified

- Real tree: 78/78 pass — the live auth surface is clean, no open route
  found on either surface.
- Negative proof (doctored copy, not the repo): stripping the
  `_authed()` guard from `/api/host-stats` fails, and adding an
  unclassified `/api/debug-dump` route fails — the gate is not vacuous.

## When adding a route

1. Add the route to the expected set in the test with its auth class.
2. Bump the exact-count grep in `deploy-all.sh` preflight if the check
   count changes, and the APPLY.md suite list.
3. The queue-audit fails closed if this package's test is not invoked in
   preflight exactly once — do not skip the wiring.

## Standalone run

```
python3 pending-deploy/route-auth-census/test_route_auth_census.py
# -> PASS=78 FAIL=0
```
