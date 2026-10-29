# input-sink-gate — deploy notes

## What it is

Manager-only hardening, same audit class as t161–t165: `do_instance_input()`
(the shared input core for the HTTP input route and the WebRTC data
channel) now fails closed on an invalid instance name — returns
`(False, {"error": "instance not found"})` before any wing call, FIFO
open, or waydroid_exec fallback. Previously the local branch only failed
closed indirectly (container_status -> docker_inspect's wd- gate made a
hostile name read as "not running" before the FIFO path was opened) and
the remote branch had no gate at all (the name went straight into the
wing_request URL path). All input routes already gate names; this makes
the sink itself safe. Valid names behave identically.

## Files changed (in the repo, deployed from HEAD via apply-all)

- `manager/app.py` — gate + docstring in `do_instance_input()` only.
- No agent, dashboard, or wire-protocol change.

## Tests

`test_input_sink_gate.py` — 44/44 checks (hermetic, real function):
hostile-name matrix blocked on both branches with zero wing calls /
zero FIFO opens / zero waydroid_exec calls; valid remote tap still
proxies with the exact wing URL; valid local tap still writes the exact
FIFO path and scaled command; stopped / FIFO-failure fallback / input
validation unchanged; gate placement + sibling sink gates kept + route
gates kept + snapshot parity; negative proof that the pre-fix remote
flow reaches wing with the hostile URL.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight t166).
No new stage files — `manager/app.py` is already in the apply-all stage
list. Standalone fallback (only if apply-all is unavailable):

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify after deploy

- `systemctl is-active jerusalem` → `active`.
- Dashboard root returns HTTP 200.
- Tapping in a viewer still moves the instance (input path exercises the
  same `do_instance_input()` sink, local and remote).
- A corrupted instance key can no longer drive a wing URL or a FIFO open
  from the shared input core (code inspection: the gate precedes the
  provider branch in `do_instance_input()`).
