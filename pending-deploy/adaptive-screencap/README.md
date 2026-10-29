# adaptive-screencap

Idle capture-rate throttling for the in-container screencap loop.

**Why:** the old loop PNG-encoded the framebuffer at 8fps on all 20
instances 24/7. The dashboard cards show no thumbnails, so with no viewer
attached that CPU bought nothing.

**How:** `waydroid-image/screencap-loop.sh` checks the mtime of
`/data/media/0/jerusalem/stream_wanted` each iteration — fresh (<30s)
means a viewer is attached → 0.12s captures; otherwise 2s. The manager
touches the marker on ws-viewer open/close (+ 15s re-touch from
`resource_poller`); the wing agent (1.0.6) touches it on every served
screenshot fetch and sweeps it after 90s idle. A stale marker degrades to
slow mode by itself, so a crashed manager/agent can't pin the loop fast.

**Files:**
- `test_adaptive_screencap.py` — 42 hermetic checks (manager + agent + wiring)
- `test_screencap_loop.sh` — 5 hermetic checks running the real loop script
- `push-screencap.sh` — one-shot live-container push (VPS root, manual)
- `DEPLOY.md` — problem / changes / tests / deploy / verify
