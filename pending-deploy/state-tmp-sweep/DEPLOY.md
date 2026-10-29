# state-tmp-sweep — DEPLOY.md

## What
The manager state dir had no crash-window cleanup at all. Every
`save_*` writes `FILE + ".tmp"` then `os.replace()`s it into place, so
a manager crash mid-write leaves the tmp behind forever — and two of
the leftovers are credential-bearing: `roblox_accounts.json.tmp` holds
every fleet Roblox credential in plaintext, `tokens.json.tmp` holds
every dashboard session token, and `static_join_token.tmp` holds the
join token. The instance-volume sweep never looked here, and
`_recover_batch_jobs()` skips non-`.json` names, so
`batch_jobs/<jid>.json.tmp` files from a crash mid-journal were
orphaned the same way. A second hole rode along: `save_tokens` never
chmodded anything, so `tokens.json` sat world-readable permanently,
and `save_accounts` chmodded AFTER the replace, so the live file sat
world-readable between the two calls.

## Fix
- `manager/app.py` `sweep_stale_state_tmp()`: removes exactly the six
  known state files' `.tmp` siblings plus journal files matching
  `<16-hex-jid>.json.tmp` — flat listing, name-gated patterns only,
  never a live file, never recursive. Returns the removal count,
  never raises. `main()` runs it at startup before the server binds
  (no save can be in flight), next to the login-volume sweep, with a
  `[state] swept N ...` log line when it removes anything.
- `save_accounts` chmods its tmp to 600 BEFORE the replace
  (`os.replace` preserves the tmp's mode, so the live file is 600
  from birth); `save_tokens` gains the same chmod-before-replace
  (was never 600). No agent change, no version cascade.

## Deploy
Rides apply-all from HEAD — `manager/app.py` is already in the stage
list. No new stage files, no restart beyond the normal
`systemctl restart jerusalem` that apply-all performs.

Standalone (SSH-capable session, manager host):
```
scp manager/app.py root@<vps>:/root/jerusalem/manager/app.py
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/manager/app.py && systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify
- `systemctl is-active jerusalem` -> `active`.
- After restart, the manager log shows no `[state]` line on a clean
  tree (nothing stale) — or one `[state] swept N ...` line if tmps
  were left by the previous process.
- `ls /root/jerusalem/*.tmp /root/jerusalem/batch_jobs/*.tmp` -> no
  matches (pre-fix, a crashed run left them indefinitely).
- `stat -c %a /root/jerusalem/roblox_accounts.json
  /root/jerusalem/tokens.json` -> `600 600` (pre-fix, tokens.json
  was 644 permanently).
- Dashboard login still works (session tokens still save/load), and
  the Accounts panel still loads (credential file still saves/loads).

## Tests
`python3 pending-deploy/state-tmp-sweep/test_state_tmp_sweep.py`
— hermetic real-app import, tmp state dir + journal dir;
exact-count line `PASS=30 FAIL=0`.
