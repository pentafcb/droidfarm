# DEPLOY: instance-record-guard

## What

The instance-side twin of t154 (flagged as the next audit candidate in
the t154 entry): t152/t153 fixed the instances FILE shape and
unreadability and t154 fixed PROVIDER records, but an INSTANCE record
inside a valid dict file can still be the wrong shape — a hand edit or
partial write can leave a string record, a string provider, or an
empty-string provider. Every caller that trusted the record shape then
crashed the handler thread with no response:

1. `instance_single_action`: `instances[name].get("provider")` raised
   AttributeError on a string record (start/stop/restart/launch/
   login-all dropped for that instance).
2. GET /api/instances loop: one string record raised AttributeError on
   `inst.get("provider")` and killed the WHOLE fleet poll; a string
   provider record crashed the pname lookup the same way.
3. `_handle_input` / `_handle_webrtc_offer` / `_handle_ws` /
   `_handle_ws_h264` / resize route / roblox-login route / DELETE
   instance: the `load_instances().get(name, {}).get("provider")`
   idiom crashed on a string record (the {} default only covers a
   MISSING record).
4. `resize_local` / `resize_remote` write-backs: `inst["memory_mb"] =
   ...` raised TypeError on a string record after docker/agent
   accepted the resize.
5. `create_remote_instance` IP pick: an agent returning a dict (not a
   list) for "instances" iterated string keys, and a string item
   crashed on `.get("ip")` — the create request dropped.
6. Create-path helpers `next_instance_id` / `used_binders` /
   `next_free_ip` and the `pick_provider` / `fleet_headroom` sums: all
   called `.get` on every instance record, so one bad record dropped
   a create, an auto-pick, or a fleet plan.

Now (manager/app.py only, no agent change, no dashboard change, no wire
protocol change): `instance_provider_of(record)` — a missing record or
a missing provider field means "local" (the historic default); a
non-dict record, or a present but non-string/empty provider, is invalid
-> None, and every caller treats None as "instance not found" (never as
local, never as a crash). The /api/instances loop skips bad records
(like the t154 providers list); remote provider records are re-gated
to dict-or-404 at the single-action, webrtc, ws, login, and delete
sites; the create-remote IP pick only counts a list of dicts. Valid
records behave identically. The wing agent is untouched (still 1.0.45),
so no VERSION bump and no agent self-update rides this package.
Remote-autologin app.py snapshot refreshed to HEAD.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py`, so no new stage files. The preflight runs
`pending-deploy/instance-record-guard/test_instance_record_guard.py`
as t155 (exact-count `PASS=49 FAIL=0`).

Standalone fallback (SSH-capable session only): copy
`manager/app.py` to `/root/jerusalem/app.py` on the VPS, then
`systemctl restart jerusalem` and verify with `systemctl is-active
jerusalem` plus a dashboard load. After deploy, a corrupt instance
record degrades to "instance not found" / skipped-list-row instead of
dropping requests, and a corrupt agent instances payload no longer
drops a remote create.

## Verify

- `python3 pending-deploy/instance-record-guard/test_instance_record_guard.py`
  prints `PASS=49 FAIL=0` (regression proof, helper matrix, real
  single-action/resize/create-IP-pick with stubbed docker/wing, real
  create-path helpers + pick + headroom, real roblox_login provider
  branch + do_instance_input node guard, source contracts + snapshot
  parity).
- Full `--stage-only` dry run green (queue-audit 0 findings, all
  suites green, py_compile + bash -n + JS gate clean).
