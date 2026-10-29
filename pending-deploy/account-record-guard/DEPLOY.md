# DEPLOY: account-record-guard

## What

The account-side twin of t155 (flagged as the next audit candidate in
the t155 entry): t152/t153 fixed the state FILE shapes and t154/t155
fixed provider and instance records, but an ACCOUNT record inside a
valid roblox_accounts.json dict can still be the wrong shape — a hand
edit or partial write can leave a string record. Every caller that
trusted the record shape then crashed the handler thread with no
response:

1. `bulk_save_accounts`: `acct["username"] = ...` raised TypeError on
   a string record (bulk import dropped mid-save).
2. `roblox_account_summary`: `acct.get("username")` raised
   AttributeError on a string record and killed the WHOLE fleet
   coverage poll (GET /api/roblox-accounts).
3. `_roblox_login_inner`: a NON-EMPTY string record is truthy, so the
   old `not acct` guard did not save it — `acct.get("username")`
   raised AttributeError and the login request dropped.
4. GET /api/instances/<n>/roblox-account: `acct.get` raised
   AttributeError on a string record.
5. POST /api/instances/<n>/roblox-account: item assignment raised
   TypeError on a string record (Account panel save dropped).

Now (manager/app.py only, no agent change, no dashboard change, no wire
protocol change): `account_of(record)` — a dict record passes through,
a non-dict record degrades to {} (no username, no password). Read paths
report "no account"; write paths replace the corrupt record with the
new fields (it held no credentials to preserve). Valid records behave
identically. The auto-login watcher already guarded with
isinstance(acct, dict) and is untouched. The wing agent is untouched
(still 1.0.45), so no VERSION bump and no agent self-update rides this
package. Remote-autologin app.py snapshot refreshed to HEAD.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py`, so no new stage files. The preflight runs
`pending-deploy/account-record-guard/test_account_record_guard.py`
as t156 (exact-count `PASS=34 FAIL=0`).

Standalone fallback (SSH-capable session only): copy
`manager/app.py` to `/root/jerusalem/app.py` on the VPS, then
`systemctl restart jerusalem` and verify with `systemctl is-active
jerusalem` plus a dashboard load. After deploy, a corrupt account
record degrades to "no account" / "no credentials saved" instead of
dropping requests, and the fleet coverage poll survives it.

## Verify after deploy

- GET /api/roblox-accounts returns 200 with coverage counts (no
  account record can kill the poll).
- Bulk account import saves over a corrupt record instead of
  dropping mid-save.
- A login attempt for an instance with a corrupt record returns the
  clean "no credentials saved" outcome, recorded in
  roblox_login_results.json.
