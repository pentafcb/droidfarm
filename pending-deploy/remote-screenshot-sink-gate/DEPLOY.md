# remote-screenshot-sink-gate — deploy notes

## What it is

Manager-only hardening, the re-sweep named by the 2026-10-28 ~13:44 UTC
session and the same audit class as t165 (local frame-source sink
gate) / t166 / t167: the stream routes all gate instance names
(route-name-gate), and the local frame source `capture_screenshot()`
gates them at the sink (t165) — but `get_remote_screenshot()`, the
SHARED remote frame source for the MJPEG, H.264, and WebRTC paths, did
NOT. It built its wing URL path
(`/api/agent/instances/%s/screenshot`) from the raw name, and
`wing_request()` concatenates the path onto the node URL with no
quoting — so a corrupted `instances.json` key reaching it from a
future internal caller (all three stream paths funnel through it)
would drive a wing URL outside the screenshot endpoint and pollute
the shared `(prov_id, name)` frame cache. The agent's own screenshot
route gates names too, but the sink itself was wrong regardless.

Now the sink fails closed at the top, before any cache touch or wing
call: invalid name -> None, indistinguishable from a missing frame,
so the 15s frameless guards on all three stream paths keep treating
it as "no frame". Valid names behave identically.

## Files changed (in the repo, deployed from HEAD via apply-all)

- `manager/app.py` — the gate + comment only.
- No agent, dashboard, or wire-protocol change (the agent's
  screenshot route already gates names).

## Tests

`test_remote_screenshot_sink_gate.py` — 34/34 checks (hermetic, real
function, stubbed wing_request): regression proof (contract + old
URL/cache-key construction interpolates a hostile name verbatim);
hostile-name matrix -> None with zero wing calls and zero cache
entries; valid fetch -> data + exact wing URL + GET/timeout=10/
attempts=1; cache hit / expiry / 404-drops-entry / raising-wing /
short / non-bytes data / stale-entry prune / dash-underscore-digit
names / None name; gate placement before cache key + wing call,
gate returns None, sibling sink gates kept (capture_screenshot,
do_instance_input, waydroid_exec), all 3 stream call sites unchanged,
snapshot parity; negative proof that the pre-fix flow builds the wing
URL from a hostile name.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight t169).
No new stage files — `manager/app.py` is already in the apply-all stage
list. Standalone fallback (only if apply-all is unavailable):

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify after deploy

- `systemctl is-active jerusalem` → `active`.
- Dashboard root returns HTTP 200.
- A corrupted instance name can no longer drive a wing screenshot URL
  from the shared remote frame source (hermetic suite proves the fix;
  live check: remote streams still serve frames for valid instances).
