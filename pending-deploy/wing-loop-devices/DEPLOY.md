# DEPLOY — wing-loop-devices (wing agent 1.0.12)

Fresh wing nodes had no loop-device story: the main node got persistent
provisioning + create-time self-heal + scripted verification in
loop-device-guard (t66), but `wing/install-wing.sh` only provisioned binder
and the agent's selfwatch never looked at `/dev/loop*`. A rebooted wing
node silently exposed the stock 8 loop devices, capping the node at ~4
booting Waydroid instances with no actionable error — the same failure
class as the original "no free binder devices".

## What changed

- `wing/agent.py` 1.0.11 → **1.0.12** (already in the apply-all stage list
  as `agent.py` → `/root/jerusalem/wing/agent.py`; rides apply-all from
  HEAD — no new stage files):
  - `sw_count_loop_devices()` / `sw_ensure_loop_devices()` — host-level
    guard: `modprobe loop max_loop=64` when the module isn't loaded, `mknod`
    for missing nodes, honest False when the live driver's `max_loop` is
    below floor (mknod can't grow it). Runs once per selfwatch pass
    (host-level, not per instance); unfixable pool → `loop.degraded` marker
    (reason on first line) → existing `selfwatch_state()` degraded map →
    heartbeat → dashboard Nodes "needs attention" badge, zero
    manager/dashboard changes; relief auto-clears.
  - `create_instance_linux()` self-heals the pool at create time and fails
    OPEN with a WARN in selfwatch.log (loop nodes are consumed at container
    boot, not reserved at create — mirrors the manager).
- `wing/install-wing.sh` (repo-side, NOT staged — served by the manager from
  `/root/jerusalem/wing/install-wing.sh` at `/api/install/install-wing.sh`;
  like the main-node `install.sh` it is a fresh-install script): new
  delimited `install_loop_pool()` — persistent `options loop max_loop=64`
  in `/etc/modprobe.d/loop.conf` + `loop` in `/etc/modules` (idempotent),
  immediate `modprobe loop max_loop=64` + mknod fallback, count verify with
  a WARNING below 60. Called right after the binder setup; stages are
  visible in the dashboard install-progress via `report()`.

## Deploy

1. Run `pending-deploy/apply-all/deploy-all.sh` from an SSH-capable session
   (agent.py rides it from HEAD). The preflight runs the new t67 suites.
2. Sync the installer to the manager's serve dir (it is NOT staged):
   `scp wing/install-wing.sh root@<vps>:/root/jerusalem/wing/install-wing.sh`
   — no service restart needed (served from disk per request).
3. Wing nodes get agent 1.0.12 when Penta re-runs the node installer (same
   re-run already pending for 1.0.2–1.0.11); until then they lack the loop
   guard and nothing misreports. Fresh installs get the loop pool from the
   installer immediately.

## Verify

- Manager: heartbeats from a 1.0.12 node report `"version": "1.0.12"`.
- Node: `ls -d /dev/loop[0-9]* | wc -l` ≥ 60 on a fresh wing node.
- Dashboard Nodes modal: a node with an unfixable pool shows the red
  "needs attention" badge with the loop reason; Eventos modal shows the
  provisioning / DEGRADED / relieved lines.
