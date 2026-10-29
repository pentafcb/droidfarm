# DEPLOY — wing-route-name-gate (agent 1.0.26: instance-name path-traversal guard)

## What
Fixes authenticated cross-instance screen read + input injection via path
traversal in wing-agent instance-name URL segments. The manager's
route-name-gate claimed "the wing agent needs nothing" — wrong for two
sinks that build filesystem paths from the raw URL segment:
`screenshot_bytes_linux()` and `_find_fifo()` (via `input_linux()` /
`launch_roblox_linux()`). A percent-encoded traversal name
(`a%2F..%2F..%2F..%2F..%2F..%2Fvar%2Flib%2Fdocker%2Fvolumes%2Fwd-rbx2`,
which sails through the `([^/]+)` route regex because `unquote` runs after
the match) resolved to ANOTHER instance's `live.png` / `input.fifo`.

`valid_agent_instance_name()` (`^[A-Za-z0-9_-]{1,32}$`, text-identical to
the manager's `_INSTANCE_NAME_RE` — the exact create-time contract) now
gates all 8 instance-name route captures in `do_GET`/`do_POST`/`do_DELETE`
-> 404 `{"error": "instance not found"}`; the three filesystem sinks refuse
invalid names as defense in depth. Agent 1.0.25 -> 1.0.26.

## Deploy
Rides apply-all from HEAD — `wing/agent.py` is already in the stage list.
No new stage files. After the manager deploys, wing nodes pick up 1.0.26
via the agent self-update (1.0.24+) on the next heartbeat; the dashboard
Nodes modal badges the update until then.

Standalone (SSH-capable session only):
```
scp wing/agent.py root@<node>:/opt/wing/agent.py   # or the node's install dir
ssh root@<node> 'python3 -m py_compile /opt/wing/agent.py && systemctl restart wing-agent'
```

## Verify
1. Agent `/api/agent/health` -> 200, `"version": "1.0.26"`
2. Authenticated: `GET /api/agent/instances/<name>/screenshot` with a valid
   name still serves; with a traversal name
   (`/api/agent/instances/a%2F..%2F..%2Fwd-rbx2/screenshot`) -> 404
   `{"error": "instance not found"}` (no filesystem access)
3. `POST /api/agent/instances/<traversal>/input` -> 404 (not 500)
4. Dashboard Nodes modal clears the agent-update badge after the heartbeat

## Rollback
Restore the previous `agent.py` (the self-update keeps one `.bak`) and
restart the agent service.
