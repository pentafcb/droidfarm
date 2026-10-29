# wing-create-preflight — agent 1.0.18 create image preflight

## What
The wing agent's instance-create path had no equivalent of the manager's
`waydroid_image_present()` fail-fast (2026-10-13 create-preflight). On a
wing node where `waydroid-instance:latest` was never built, `docker run`
failed deep with a cryptic docker error ("Unable to find image ...
pull access denied") — after the network ensure and binder dance had
already run. The eighth "main had it, wing didn't" parity item.

## Changes (all in `wing/agent.py`, rides apply-all from HEAD)
- `VERSION` 1.0.17 → 1.0.18
- New `WING_IMAGE_MISSING_MSG`: actionable error naming the image and the
  `pending-deploy/waydroid-image-build` package.
- New `sw_image_present()` → True/False/None (`docker image inspect`,
  list-form, never raises; only a positive "No such image" refuses;
  daemon-down/unknown output fails OPEN so the real `docker run` error
  surfaces).
- `create_instance_linux()`: after the docker-unavailable/already-exists
  checks, before any network/binder work — `sw_image_present() is False`
  → `(False, WING_IMAGE_MISSING_MSG)`.
- No manager/dashboard change: the agent's `{"ok": False, "msg": ...}`
  already flows through `create_remote_instance`'s passthrough and the
  dashboard renders `j.msg`.

## Deploy
Rides `pending-deploy/apply-all/deploy-all.sh` (or
`pending-deploy/newbox-sync/sync-newbox.sh --yes` for the new box) from
repo HEAD — no new stage files, `wing/agent.py` is already in the stage
list. The vultr node gets agent 1.0.18 when Penta re-runs the node
installer (same re-run already pending for 1.0.2–1.0.17).

## Verify (after the SSH session's deploy)
1. `verify-live.sh --ssh` shows `wing/agent.py` matching HEAD.
2. On a wing node WITHOUT the image: `POST /api/instances` with a node
   target → 500 with the "not built on this node" message (no docker run).
3. On a node WITH the image: create proceeds as before.

## Tests
`python3 pending-deploy/wing-create-preflight/test_wing_create_preflight.py`
— 35/35 hermetic checks (units, fail-fast ordering, fail-open, message
and route contracts). Wired as apply-all preflight t73.
