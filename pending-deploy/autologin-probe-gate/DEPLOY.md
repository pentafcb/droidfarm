# autologin-probe-gate — deploy notes

## What it is

Manager-only hardening, same audit class as t161–t166: the remote
auto-login PROBE surface now fails closed on an invalid instance
name. The login sequence itself was gated at `_roblox_login_inner`
(t161), but `_auto_login_pass` iterates `roblox_accounts.json` keys
directly — no HTTP route stands between a corrupted account key and
the probes — and for a remote instance it called
`_remote_started_at(p, name)` plus an inline status wing_request with
the raw key BEFORE the login sequence's own gate ever ran.
`_wing_input` and `_roblox_login_remote`, the other two helpers that
interpolate a name into a wing URL path, had no gate either (their
only callers gated today, but the sinks themselves did not). The
local branch already failed closed indirectly (docker_inspect's wd-
gate makes a hostile name read as "not started"). Now all four sites
fail closed at the top: `_wing_input` -> (False, {"ok": False}),
`_remote_started_at` -> 0, `_roblox_login_remote` -> (False,
"instance not found"), `_auto_login_pass` skips invalid keys before
any probe, save, or thread spawn. Valid names behave identically.

## Files changed (in the repo, deployed from HEAD via apply-all)

- `manager/app.py` — four gates + comments only.
- No agent, dashboard, or wire-protocol change.

## Tests

`test_autologin_probe_gate.py` — 64/64 checks (hermetic, real
functions): hostile-name matrix blocked in all three helpers with
zero wing calls (login helper: zero provider lookups too); valid
paths unchanged (exact wing URLs, agent version gate, not-running
guard, full remote login sequence with base64-only password on the
wire); real `_auto_login_pass` — hostile remote/local keys give zero
wing calls, zero saves, zero spawns, valid remote ready gives one
spawn + attempts save with the exact probe URLs, not-ready /
already-logged-in / local-stopped give no spawn; gate placement +
sibling sink gates kept + route gates kept + snapshot parity;
negative proof that the pre-fix probe flow reaches wing with the
hostile URL.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight t167).
No new stage files — `manager/app.py` is already in the apply-all stage
list. Standalone fallback (only if apply-all is unavailable):

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify after deploy

- `systemctl is-active jerusalem` → `active`.
- Dashboard root returns HTTP 200.
- A corrupted account key can no longer drive a wing URL from the
  auto-login watcher (code inspection: the gate is the first statement
  in the `_auto_login_pass` loop, and each remote helper gates before
  its first agent call).
- Remote auto-login still fires on a real first boot (needs a live
  node + account; covered by the existing auto-login watcher path,
  unchanged for valid names).
