# login-script-600

The last credential-adjacent manager write left unhardened after
the state-600-complete audit: `manager/app.py`
`_roblox_login_inner` wrote the staged `auto_login.sh` with a bare
`open("w")` — no tmp+rename, no chmod — into the instance's
shared media dir, readable from inside Android by any app while
the login ran and until the startup sweep after a crash mid-login.
The script carries the account username; the `pwd.txt` next to it
already had the tmp+chmod+replace treatment, the script did not.
The startup sweep also missed the script's `.tmp` sibling, so a
crash between the tmp write and the rename would leave the
username behind in a file nothing swept.

Now the script is written to `auto_login.sh.tmp`, chmodded to 600
BEFORE the replace (OSError-guarded), the write-failure path
removes the tmp too, and the sweep removes `auto_login.sh.tmp`.
The script runs via `sh <path>` as root, so 600 stays readable
to the login flow.

Tests: `python3 test_login_script_600.py` — 33/33 hermetic (real
app import, tmp state + volume dirs, umask 022, docker/waydroid
faked). Wired as preflight t129. Rides apply-all from HEAD; see
DEPLOY.md.
