# DEPLOY — fleet batch Roblox login ("Login all")

## What it is
Dashboard fleet bar gains a "Login all" button; the batch engine gains a
`roblox-login` action. One tap fires the saved-account login sequence on every
instance (sequentially, no thundering herd); outcomes are recorded with
by="batch" so the dashboard login badges + "K logged in" fleet coverage update
when the job finishes.

## Files changed (all already in the apply-all stage list — rides it from HEAD)
- manager/app.py: `BATCH_ACTIONS` += "roblox-login"; `record_login_result`
  accepts by="batch"; `instance_single_action` gains the roblox-login branch
  (local: 409 when stopped; remote: driven by roblox_login, NOT proxied to
  the wing agent — there is no agent-side login endpoint).
- manager/static/index.html: btnFleetLogin button + onclick + comment.

## Tests
`python3 pending-deploy/batch-login/test_batch_login.py` — 40/40 pass.

## Deploy
No new stage files — app.py + index.html are already in the apply-all stage
list. Run `bash pending-deploy/apply-all/deploy-all.sh` from an SSH-capable
session. Standalone fallback:
  scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
  scp manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html
  ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'

## Verify
1. `systemctl is-active jerusalem` -> active; dashboard HTTP 200.
2. Dashboard fleet bar shows "Login all" between "Roblox all" and "Accounts".
3. POST /api/instances/batch {"action":"roblox-login"} -> 202 with job_id;
   GET /api/instances/batch/<job_id> reaches status done; per-instance results
   have ok/error; instances without accounts report "no credentials saved".
4. GET /api/roblox-accounts summary -> logged_in count updated; instance cards
   show login badges with the new attempts.
5. Unauthenticated POST /api/instances/batch -> 401.
