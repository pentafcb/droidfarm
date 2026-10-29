# remote-headroom — wing-node image/loop data for the Create-fleet plan preview

## Problem

The fleet-create plan preview (`POST /api/instances/fleet/plan` →
`fleet_headroom()`, remote branch) only knew a wing node's heartbeat RAM
stats. It could not warn that a create would fail on a node whose
`waydroid-instance` image was never built, or whose loop pool is the stock 8
— the exact failures hit on new/vultr nodes (the manager fails fast at
create time via `create-preflight` locally and agent 1.0.18's
`sw_image_present()` on the node, but the plan preview is where Penta
decides, and it showed nothing).

## What changed

- `wing/agent.py` **1.0.20 → 1.0.21**: new authed
  `GET /api/agent/headroom` → `{"ok": true, "image_present": true/false/null,
  "loop_devices": int|null}`. `image_present` reuses the 1.0.18
  create-preflight tri-state (fail-open, never raises); `loop_devices`
  reuses the 1.0.12 counter (None on Windows). Binder needs no plan-time
  accounting on wing nodes (the agent self-heals the pool at create, 1.0.16).
- `manager/app.py`: new `probe_remote_headroom(hr, provider_id)` fills
  `image_present` / `loop_devices` on the remote branch, fail-fast
  (`timeout=5, attempts=1` — the plan is a user-initiated dry run, never the
  10s poll). `headroom_probe` is `"ok"`, `"old-agent"` (404 → agent < 1.0.21,
  warning names the installer re-run), or `"unreachable"` (stale heartbeat
  or fetch failure). The image-missing warning is node-aware
  ("not built on node srt-node"). All helpers best-effort, never raise.
- Dashboard: **no change needed** — the plan modal already renders
  `headroom.warning`.

## Deploy

Rides `apply-all` from HEAD — `wing/agent.py` + `manager/app.py` are already
in the stage list. After deploy, the vultr node needs Penta's installer
re-run for agent 1.0.21 (until then the plan shows the "old-agent" warning,
which is the honest state).

## Verify

- `python3 pending-deploy/remote-headroom/test_remote_headroom.py`
  → `40 passed, 0 failed`
- `fleet-create` 71/71, `create-preflight` 22/22, `loop-device-guard` 55/55
  (in-suite contract updates for the new remote-branch behavior)
- Full `deploy-all.sh --stage-only` dry run green (preflight t77)

## Tests

`test_remote_headroom.py` (hermetic + one live-localhost agent server):
VERSION exact bump + manager gate, agent route contract (source + live:
200/auth/401s, payload shape), `probe_remote_headroom` matrix (healthy,
image-missing, low-loop, garbage payload, 404/500/raise, stale heartbeat,
unknown provider, garbage record, fail-fast attempts=1), `fleet_headroom`
remote integration (all warning texts, node-aware image text, local-branch
regression, additive `headroom_probe` key), dashboard contract.
