# Deploy: read-body-missing

Rides apply-all from HEAD — no new stage files (`manager/app.py` is already
in the stage list). No service restart is strictly required for a
`Handler` method addition, but apply-all restarts the manager anyway, so
this ships with the next `deploy-all.sh` run.

## Verify after deploy

1. The manager service is active (systemctl is-active on the box's manager unit).
2. Dashboard login works, then in a viewer: Account Save returns success
   and the saved username persists on reopen (previously the request
   dropped with no response).
3. Instance Resize with valid values reaches the docker-update path
   (previously dropped); invalid values return a 400 JSON error.
4. `python3 pending-deploy/read-body-missing/test_read_body_missing.py`
   → `PASS=20 FAIL=0` (run from the repo before deploying, as preflight t119).

## Rollback

Re-deploy the previous apply-all stage (vps-apply.sh restores timestamped
backups automatically on a failed verify). The pre-fix behavior is just the
two broken routes again — no state migration is involved.
