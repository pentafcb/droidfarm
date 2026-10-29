# deploy-rerun-fix — deploy-all.sh re-run executed the FIRST run's stage (BUILT + TESTED, rides apply-all)

## The bug

`deploy-all.sh` uploaded the stage with `scp -r /tmp/jerusalem-deploy-<ts> $VPS:/root/jerusalem-deploy/`
and then ran `bash /root/jerusalem-deploy/vps-apply.sh` over ssh.

`scp -r` has layout-dependent behavior:
- target `/root/jerusalem-deploy/` **missing** (first run) → the source dir is
  **renamed** onto it: `/root/jerusalem-deploy/{stage,vps-apply.sh,MANIFEST}`.
  The ssh command works.
- target **exists** (every re-run) → the source dir is **nested** inside it:
  `/root/jerusalem-deploy/jerusalem-deploy-<ts>/{stage,vps-apply.sh,MANIFEST}`.
  The ssh command still addresses `/root/jerusalem-deploy/vps-apply.sh` — the
  **first run's** script and stage.

A re-run therefore silently re-deployed the previous run's code while the log
claimed the new git hash (verify passes: the old code is healthy). The old
VPS (209.222.10.93) already had `/root/jerusalem-deploy` from the 2026-10-09
run, so the next `deploy-all.sh` there — and any second `sync-newbox.sh
--yes` on the new box — would have hit this.

## The fix (`pending-deploy/apply-all/`)

- `deploy-all.sh` main: `mkdir -p /root/jerusalem-deploy` on the VPS **before**
  scp (deterministic nesting on every run), then the ssh command addresses the
  just-uploaded run dir: `bash /root/jerusalem-deploy/jerusalem-deploy-<ts>/vps-apply.sh`.
  `--stage-only` prints the same three commands.
- `vps-apply.sh`: new `prune_old_runs()` after a successful apply — keeps the
  newest 3 timestamped run dirs under `/root/jerusalem-deploy/`, deletes older
  ones. Strict guards (only when this script lives in
  `.../jerusalem-deploy/jerusalem-deploy-<ts>/`, exact timestamp pattern on
  siblings), warn-only so pruning can never fail a deploy. Rollback runs keep
  their dirs for forensics (prune only runs on success).

## Tests

`test_apply_all.sh` gains a fake scp/ssh transport with **real** `scp -r`
nesting semantics, plus two end-to-end tests (62/62 pass):
- `t_deploy_all_rerun_fresh_stage`: a previous run's dir exists with a STALE
  stub `vps-apply.sh` that marks itself if executed. Asserts: exit 0, the
  stale stub never ran, the NEW stage's `app.py` deployed, ssh addressed the
  timestamped run dir, and 4 seeded old run dirs pruned to 3 (the two oldest
  gone). Fails against the pre-fix script (ssh addressed the flat path).
- `t_deploy_all_first_run_nested`: no `/root/jerusalem-deploy` seeded —
  asserts the mkdir makes the layout deterministic and the new stage runs.

## Deploy

No new stage files — `deploy-all.sh`/`vps-apply.sh` ride apply-all from HEAD.
The fix is effective the next time `deploy-all.sh` (or `sync-newbox.sh --yes`,
which calls it) runs from the repo machine. Nothing to run on the VPS by hand.
