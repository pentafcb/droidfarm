# wing-route-name-gate — agent 1.0.26 instance-name path-traversal guard

## The gap
The manager's route-name-gate (2026-10-19 ~20:25 UTC) fixed shell injection
in its own routes and closed with "the wing agent needs nothing (its
`docker()` is list-form, its marker readers already fullmatch-reject)".
That claim was wrong for two sinks that build **filesystem paths** from the
raw URL segment:

| route | sink |
|---|---|
| `GET /api/agent/instances/<name>/screenshot` | `screenshot_bytes_linux()` -> `/var/lib/docker/volumes/wd-<name>-data/_data/.../live.png` |
| `POST /api/agent/instances/<name>/input` | `input_linux()` -> `_find_fifo()` -> `.../input.fifo` (os.open O_WRONLY) |
| `POST /api/agent/instances/<name>/launch-roblox` | `launch_roblox_linux()` -> same fifo |

The route regex `([^/]+)` excludes literal `/` but the handlers `unquote()`
*after* matching, so percent-encoded slashes sail through:
`GET /api/agent/instances/a%2F..%2F..%2F..%2F..%2F..%2Fvar%2Flib%2Fdocker%2Fvolumes%2Fwd-rbx2/screenshot`
resolves (verified via `os.path.normpath`) to
`/var/lib/docker/volumes/wd-rbx2-data/_data/data/media/0/jerusalem/live.png`
— rbx2's screen served for a garbage instance name. The same traversal on
`/input` opens another instance's `input.fifo` for writing.

Severity (honest): authenticated-only (agent-key HMAC on every route), no
RCE (`docker()` is list-form; fifo writes are constrained `input ...`
command strings). Impact is cross-instance screen read + cross-instance
input injection for a key holder hitting the agent directly — the manager
path was already gated. Defense in depth + audit correctness, and it
corrects the audit claim.

Not affected: `docker("logs"/"start"/"stop"/"rm", ...)` (list-form, weird
names just error), `delete_instance_linux`'s dict pop, `instance_status`
(blunted: `container_status` exact-matches container names, a traversed
`cname` never matches -> "missing" -> stopped dict), the selfwatch helpers
(all fullmatch-reject already), `create_instance_linux` (fullmatch-rejects).

## The fix (`wing/agent.py`, 1.0.25 -> 1.0.26)
- `valid_agent_instance_name()` — `^[A-Za-z0-9_-]{1,32}$`, the regex text
  identical to the manager's `_INSTANCE_NAME_RE` (create-time contract).
  Never raises.
- All 8 instance-name route captures (`do_GET`: screenshot/status/logs;
  `do_POST`: start/input/launch-roblox/start|stop; `do_DELETE`: instance)
  gate right after `unquote(...)`: invalid -> `404 {"error":
  "instance not found"}`, indistinguishable from an unknown instance.
- Defense in depth at the sinks: `screenshot_bytes_linux()` returns None,
  `input_linux()`/`launch_roblox_linux()` return False on invalid names —
  so a future caller can't reintroduce the traversal.

## Tests
`test_wing_route_name_gate.py`, 33/33: helper units (incl. never-raises),
the exact exploit regression (traversal name -> sink refuses with
`os.path.exists`/`os.open` monkeypatched to fail loudly if called),
source contracts (every capture gated before its sink), create-contract
parity, manager regex-text parity, sink-gate persistence.

## Wiring
Preflight t83 (exact-count `"33 passed, 0 failed"`); APPLY.md table row +
suite lists (83 suites); 21 suites' VERSION tuples widened to 1.0.26
(wing-selfwatch-lock's exact-bump -> 1.0.25+); remote-autologin snapshots
refreshed to HEAD. No new stage files — `wing/agent.py` already staged.
