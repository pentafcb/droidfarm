# deploy-rerun-fix

Fixes a stale-deploy bug in the deploy orchestrator itself: `deploy-all.sh`
re-runs executed the FIRST run's `vps-apply.sh` + stage because `scp -r`
nests into an existing target dir while the ssh command addressed the flat
path. Now every run `mkdir -p`s the remote base first (deterministic nested
layout) and executes the run dir it just uploaded; old run dirs prune to 3
after a successful apply.

- Changes: `pending-deploy/apply-all/deploy-all.sh` (mkdir + timestamped
  remote path, `--stage-only` manual commands), `pending-deploy/apply-all/vps-apply.sh`
  (`prune_old_runs()` on success).
- Tests: `pending-deploy/apply-all/test_apply_all.sh` — fake scp/ssh with real
  nesting semantics; re-run (stale-stage regression) + first-run tests.
  62/62 pass.
- Rides apply-all from HEAD (no new stage files). Effective on the next
  `deploy-all.sh` / `sync-newbox.sh --yes` run from the repo machine.
