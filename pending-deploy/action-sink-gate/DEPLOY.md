# DEPLOY — action-sink-gate (name gate on the start/stop/restart core)

## What it is
`manager/app.py` gains a fail-closed gate at the top of
`instance_single_action`, the shared start/stop/restart core — the
follow-up the t163 (resize-sink-gate) session's audit class pointed
at, on the fleet-action path this time.

**The gap**: the per-instance routes gate the instance name
(`valid_instance_name`) before calling `instance_single_action`,
but the shared core itself did NOT — its only guard was membership
in `instances.json`, and the batch worker validates job names the
same way (membership only). A hand-edited or corrupted state key
(e.g. `rbx1;id>/tmp/p`) present in `instances.json` would flow
verbatim into the shell `docker start`, `docker stop`, and restart
`docker stop`/`docker start` commands, and into the wing_request URL
path on the remote branch. The login (t161), create (t162), and
resize (t163) sinks are gated; the start/stop/restart core was the
remaining ungated shell sink in the shared path.

**The fix**: the function fails closed at the top, before any
docker call or state read — invalid name ->
`(404, "instance not found")`, indistinguishable from an unknown
instance, exactly like the routes. Valid names behave identically
(all real instance ids match the contract). The agent side needs no
change (its docker calls are list-form and its routes already
fullmatch-reject names). No dashboard change, no wire-protocol
change.

## Tests
`test_action_sink_gate.py` — 53/53 pass (real function, stubbed
loads/run/docker helpers): regression proof (contract fact + old
shell construction interpolates a hostile state key verbatim),
hostile keys present in `instances.json` -> 404 with zero docker
calls (start/stop/restart/launch-roblox/roblox-login) and zero wing
calls (remote), unknown valid name -> 404, valid start/stop/restart
-> 200 with the exact docker commands in order, valid remote start
still proxies, launch-roblox / roblox-login / stopped-instance 409 /
unknown-action 400 preserved, source contracts (gate first in the
function, before docker strings and the wing branch, sibling sink
gates kept, route gates kept, snapshot parity), negative proof
(reconstructed pre-fix flow DOES reach docker with the hostile
command).

## Deploy (rides apply-all — no new stage files)
`manager/app.py` is already in the apply-all stage list; the gate
deploys from repo HEAD. Run from an SSH-capable session:

```bash
bash ~/workspace/droidfarm/pending-deploy/apply-all/deploy-all.sh
```

Standalone (if apply-all is unavailable): scp `manager/app.py` to
`/root/jerusalem/app.py` on the VPS, then `python3 -m py_compile`
it and `systemctl restart jerusalem`, and verify the service is
active. A start/stop on a corrupted instance key must then return
404 without touching docker; a valid start/stop still works live.
