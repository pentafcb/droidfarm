# remote-headroom

Gives the Create-fleet plan preview real data about wing nodes.

Before: `fleet_headroom()` on a remote provider only knew heartbeat RAM
stats — the plan couldn't warn that `waydroid-instance` was never built on
the node or that its loop pool was the stock 8, so Penta discovered it when
the creates failed.

After: agent 1.0.21 serves `GET /api/agent/headroom`
(`image_present` tri-state, `loop_devices`); the manager probes it fail-fast
at plan time and the existing `headroom.warning` render shows:

- image missing → "the waydroid-instance image is not built on node
  `<name>` — create will fail until the image is built
  (pending-deploy/waydroid-image-build)."
- loop < 60 → the existing loop-floor warning
- agent < 1.0.21 (404) → "node agent predates the headroom probe (1.0.21) —
  image/loop headroom unknown; re-run the node installer…"
- node unreachable → "could not reach the node for headroom details —
  image/loop state unknown; create may fail partway."

Eleventh "main had it, wing didn't" parity item (the manager's
create-preflight image check, 2026-10-13, never had a plan-time wing
equivalent).
