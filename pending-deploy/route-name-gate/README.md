# route-name-gate — shell-injection guard on instance-name routes

## The gap
Six authenticated manager routes interpolated the raw URL path segment
(`([^/]+)` — excludes `/` but allows `; $() `` | &` and friends) into
`subprocess.run(..., shell=True)` docker commands running as root:

| route | sink |
|---|---|
| `GET /api/instances/<name>/roblox` | `check_roblox` -> `waydroid_exec` |
| `POST /api/instances/<name>/roblox-login` | `container_status` -> `docker_inspect` |
| `POST /api/instances/<name>/input` | `do_instance_input` -> `container_status` |
| `DELETE /api/instances/<name>` | `run("docker stop wd-<name>")` (+ rm, volume rm) |
| `GET /ws/screen/<name>` | `container_status` |
| `GET /ws/h264/<name>` | `container_status` |

`POST /api/instances/<name>/roblox-login` with name `;touch${IFS}/tmp/x;#`
executed the injected command as root. `instance_single_action` was safe
(dict-membership 404); the batch-resume work had already flagged this class
for journal names, but the route layer was never gated.

## The fix (`manager/app.py`)
- `valid_instance_name()` — `^[A-Za-z0-9_-]{1,32}$`, the exact create-time
  contract (single-create strips anything else, fleet-create generates
  `prefix+N`, the agent's create fullmatch-rejects). Never raises.
- Every route capture (10 HTTP + 2 ws handlers) gates right after
  `unquote(...)`: invalid -> `404 {"error": "instance not found"}`,
  indistinguishable from an unknown instance (no oracle).
- Defense in depth at the sinks: `docker_inspect` only accepts
  `wd-<valid>`; `waydroid_exec` only accepts a valid bare name — so a
  hand-edited `instances.json` can never inject either.

## What it does NOT change
- No new staged files; rides apply-all from HEAD (`app.py` already staged).
- No dashboard, API-shape, or behavior change for valid names (the suite
  proves create-sanitizer output always passes the gate).
- The wing agent needs nothing: its `docker()` is list-form (no shell) and
  its marker readers already fullmatch-reject.

## Tests
`test_route_name_gate.py` — 35 checks, hermetic (fake subprocess, no
network/docker): helper units incl. never-raises, sink exploit-regression
(hostile names never reach a subprocess), create-parity, and source
contracts that all 12 route sites carry the gate.
