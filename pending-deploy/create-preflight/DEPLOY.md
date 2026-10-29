# create-preflight — fail-fast when the Waydroid image is missing

## Problem
On a host where the `waydroid-instance:latest` image was never built (the new
VPS 64.177.49.68), creating an instance failed deep inside `docker run` with
a cryptic docker error, AFTER the instance had been reserved in
`instances.json` (the reservation is rolled back, but the error told Penta
nothing actionable). The fleet-create plan endpoint also gave no warning.

## Change (manager/app.py only)
- `waydroid_image_present()` → True / False / None. `docker image inspect`
  answering "No such image" is the only refusal; anything indeterminate
  (daemon down, unexpected output, exception) returns None and fails OPEN so
  the real `docker run` error surfaces instead of a false refusal. Never raises.
- `create_local_instance()`: when the image is positively missing, rolls back
  the reservation (same pattern as the docker-run-failure path) and returns
  `500 {"error": <actionable message>}` before `docker run` is attempted.
  Shared by the single-create endpoint and the fleet-create worker.
- `fleet_headroom()` (local branch only — wing nodes own their docker):
  new `image_present` field + a plan warning when the image is missing, so
  the dashboard plan preview shows it BEFORE anything is created. The
  dashboard already renders `headroom.warning`; no dashboard change needed.

## Deploy
Rides apply-all from HEAD (no new stage files — `manager/app.py` already in
the stage list).

Standalone (SSH-capable session):
```
scp manager/app.py root@<host>:/root/jerusalem/app.py
ssh root@<host> 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```
New box (64.177.49.68): covered by `pending-deploy/newbox-sync/sync-newbox.sh --yes`.

## Verify
- `python3 pending-deploy/create-preflight/test_create_preflight.py` → 22 passed, 0 failed
- On a host without the image: dashboard Create → clear "image not built"
  error, no instance reserved. Fleet plan → image warning in red.
- On a host with the image: creates proceed unchanged.
