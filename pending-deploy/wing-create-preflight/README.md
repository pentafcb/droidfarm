# wing-create-preflight (agent 1.0.18)

Fail-fast when a wing node has no `waydroid-instance` image — the
eighth "main had it, wing didn't" parity item (manager got
`waydroid_image_present()` on 2026-10-13).

## Why
Creating an instance on a wing node that never built the image produced a
cryptic `docker run` failure deep in the create path, after the docker
network ensure and the binder self-heal had already run. The manager
refuses the same situation locally with an actionable 500 ("build the
Waydroid image first"). The agent now does the same, before any
network/binder writes:

- `sw_image_present()` — tri-state `docker image inspect` probe:
  True (present), False (positive "No such image"), None (could not tell —
  daemon down, unexpected output). None fails OPEN so the real `docker run`
  error surfaces instead of a false refusal. Never raises; list-form
  docker call, no shell interpolation.
- `create_instance_linux()` calls it right after the docker-unavailable /
  already-exists checks and returns `(False, WING_IMAGE_MISSING_MSG)` on
  False. The message names the image and the
  `pending-deploy/waydroid-image-build` package, and it reaches the
  dashboard through the existing `{"ok": False, "msg": ...}` → manager
  passthrough → `j.msg` rendering.

## Files
- `wing/agent.py` — the implementation (rides apply-all from HEAD)
- `pending-deploy/wing-create-preflight/test_wing_create_preflight.py` —
  35/35 hermetic checks
- `pending-deploy/wing-create-preflight/DEPLOY.md` — deploy + verify steps

Preflight: t73 (`"35 passed, 0 failed"` exact-count grep). Vultr gets
1.0.18 when Penta re-runs the node installer.
