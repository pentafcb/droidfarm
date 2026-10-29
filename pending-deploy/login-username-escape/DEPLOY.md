# Deploy: login-username-escape (manager/app.py security fix)

Rides `apply-all` from HEAD — `manager/app.py` is already in the stage list.
No new staged files, no VPS SSH needed for this item itself.

## Via apply-all (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh        # old VPS (default target)
# or
bash pending-deploy/newbox-sync/sync-newbox.sh --yes   # new VPS 64.177.49.68
```

This carries the fixed `_roblox_login_inner()` to `/root/jerusalem/app.py`
(the deploy restarts `jerusalem.service` once). Verify on the VPS:

```bash
grep -n 'shlex.quote(username.replace(" ", "%s"))' /root/jerusalem/app.py
grep -c '^import shlex' /root/jerusalem/app.py   # 1
```

## What it fixes

The saved Roblox username is user-typed (dashboard Account panel /
bulk import, only length-truncated). The local login path wrote it into
`auto_login.sh` with only spaces->`%s` and quote-stripping, and the script
runs under `sh` as root inside the Android container — so a username like
`x$(touch /tmp/pwned)` executed arbitrary commands in the container on
every login attempt (Login button, Login-all batch, boot auto-login).
The fix shell-quotes the username at the sink (`shlex.quote`, keeping the
`%s` space encoding `input text` needs); the password path was already
safe (double-quoted `"$P"`) and is byte-identical.

## Live behavior check (after deploy)

No live behavioral difference for legitimate usernames — the typed text is
byte-identical (spaces still `%s`-encoded). Trigger a manual Roblox login
from any viewer and confirm the username field fills as before. There is
no new log line or dashboard badge; the fix is silent by design.

## Rollback

Re-run apply-all from the previous commit; `app.py` is covered by the
orchestrator's automatic backup + rollback (single service restart).
