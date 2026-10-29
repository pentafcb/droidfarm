# wing-binderfs-selfheal — agent 1.0.16 binderfs self-heal on instance create (DEPLOY)

## What it is
Main-node parity with the manager's `ensure_binder_devices()` (2026-10-07
binderfs-selfheal): when instance creation found no free binder triplet, the
manager recreates the pool via binder-control and retries once before
failing. The wing agent's create path had no such recovery — a wing node
rebooted back to the stock binder set failed EVERY instance create with
"binder devices not found" until a human recreated the pool by hand. This
is the sixth "main had it, wing didn't" parity item (1.0.11 session-death,
1.0.12 loop devices, 1.0.13 memory, 1.0.14 stuck-boot, 1.0.15 keepalive
mirror).

## Behavior
- New `sw_find_binder_triplet()` (extracted from the create path, same
  binder/binder0 scheme detection): returns the 3 host device paths or
  None. Never raises.
- New `sw_ensure_binder_devices(count=64)`: creates binder, binder1..
  binderN via binder-control (names generated internally, no user input).
  Best-effort `modprobe binder_linux` when binder-control is missing —
  NEVER with a `devices=<count>` value (the binder 'devices' module
  parameter is a comma-separated NAME list, not a count). Mounts binderfs
  when needed. Linux only (IS_WINDOWS guard). Best-effort, never raises.
  Returns True when at least one device was created.
- `create_instance_linux` now scans -> self-heals -> re-scans once ->
  fails with "binder devices not found" only when the pool is genuinely
  unhealable. No self-heal shell writes when a triplet already exists.
- The manager's `>=1.0.2` agent gate accepts 1.0.16 (no manager change).

## Deploy
Rides apply-all from HEAD (wing/agent.py already in the stage list). The
vultr node gets agent 1.0.16 when Penta re-runs the node installer (same
re-run already pending for 1.0.2-1.0.15).

## Verify
- `python3 pending-deploy/wing-binderfs-selfheal/test_wing_binderfs_selfheal.py`
  -> `41 passed, 0 failed`.
- On a wing node with agent 1.0.16: wipe the binderfs pool back to stock
  (or reboot) -> create an instance via the dashboard -> the agent
  recreates the pool and the create succeeds; instance cards unaffected.
