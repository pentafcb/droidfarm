# login-script-600 — DEPLOY.md

## What
Closes the last credential-adjacent write left unhardened after
the state-600-complete session closed the state-file 600 audit.
`manager/app.py` `_roblox_login_inner` wrote the staged
`auto_login.sh` with a bare `open("w")` — no tmp+rename, no chmod
— into the instance's shared media dir
(`/data/media/0/jerusalem/`), readable from inside Android by any
app while the login ran, and until the startup sweep after a
manager crash mid-login. The script carries the account username
(shlex-quoted) and the login tap flow; the `pwd.txt` file next to
it already had the tmp+chmod+replace treatment, the script did
not. Its crash leftover had a second hole:
`sweep_stale_pwd_files` removed `auto_login.sh` but NOT
`auto_login.sh.tmp`, so a crash between the tmp write and the
rename (once the script goes atomic) would leave the username
behind in a file the sweep never touched.

## Fix
The script is written to `auto_login.sh.tmp`, chmodded to 600
BEFORE the replace (`os.replace` preserves the tmp's mode, so the
live file is 600 from birth; the chmod is OSError-guarded so it
never breaks a login), and the write-failure path removes the tmp
as well as `pwd.txt` and the script. The startup sweep now removes
`auto_login.sh.tmp` alongside the other three staging files. No
agent change, no version cascade. The script is run via
`sh <path>` as root in the container, so a 600 root-owned file
stays readable to the login flow.

## Deploy
Rides apply-all from HEAD — `manager/app.py` is already in the
stage list. No new stage files, no restart beyond the normal
`systemctl restart jerusalem` that apply-all performs.

Standalone (SSH-capable session):
```
scp manager/app.py root@<vps>:/root/jerusalem/manager/app.py
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/manager/app.py && systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify
- `systemctl is-active jerusalem` -> `active`.
- Dashboard instance list still loads; a login attempt still runs
  and records its outcome badge.
- After a login, no `auto_login.sh` or `auto_login.sh.tmp` remains
  in the instance volume's `data/media/0/jerusalem/` dir (both are
  removed on every exit path; the sweep covers the crash path).

## Tests
`python3 pending-deploy/login-script-600/test_login_script_600.py`
— hermetic real-app import, tmp state + volume dirs, umask 022,
docker/waydroid faked; exact-count line `PASS=33 FAIL=0`.
Pre-fix regression proof: `git show HEAD:manager/app.py` has the
bare `open(script_path, "w")` and no `auto_login.sh.tmp` in the
sweep. One first-run failure was a test bug (the space-to-`%s`
replacement makes the quoted username `touch%s/tmp`, not
`touch%/tmp`) — fixed in the tests, no product change.
