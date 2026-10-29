# Bulk Roblox account import — deploy notes

## What it is
Fleet-wide credential assignment: the dashboard gets an "Accounts" button in
the fleet bar that opens a bulk-import modal. Paste one line per instance
(`name,username,password[,auto]`), import once, and credentials land in
roblox_accounts.json for all matched instances. Unknown instance names are
refused per-line; the fleet bar shows live coverage ("14/20 have accounts").

## Files (all already in the apply-all stage list — no new package needed)
- manager/app.py — parse_bulk_accounts(), bulk_save_accounts(),
  roblox_account_summary(), GET /api/roblox-accounts, POST
  /api/roblox-accounts/bulk (both auth-gated; summary never emits passwords)
- manager/static/index.html — Accounts button, acctBulkModal, coverage span,
  wired into refresh()

## Deploy
Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD (app.py and
index.html are in the stage list). Test suite is part of the preflight:
`pending-deploy/bulk-accounts/test_bulk_accounts.py` (58 checks).

Standalone (SSH-capable session, only if needed without apply-all):
```bash
scp ~/workspace/droidfarm/manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp ~/workspace/droidfarm/manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify after deploy
1. `systemctl is-active jerusalem` -> active
2. Dashboard fleet bar shows "N/M have accounts" and an Accounts button
3. Open Accounts modal, import one line for a test instance, re-open the
   modal — coverage increments; per-instance Account panel shows the username
4. GET /api/roblox-accounts returns no "password" keys (only has_password)

## Behavior notes
- Passwords with special chars are fine (sent as JSON body, never shell).
- Lines with unknown instance names are skipped and reported; nothing is
  written if zero lines validate.
- Per-instance Account panel (viewer) is unchanged and still works for
  one-offs and clearing.
