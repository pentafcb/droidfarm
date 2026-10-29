# create-preflight

Fail-fast preflight for instance creation when the `waydroid-instance`
Docker image is missing on the host.

- `waydroid_image_present()` in `manager/app.py`: True/False/None
  (None = indeterminate → fail-open, never a false refusal).
- `create_local_instance()` refuses with an actionable 500 and rolls back
  the `instances.json` reservation before `docker run` is attempted.
- `fleet_headroom()` warns in the plan preview for local creates; wing-node
  plans are untouched (the agent owns its docker).

Tests: `test_create_preflight.py` — 22/22 hermetic.
Rides apply-all from HEAD (app.py already staged).
