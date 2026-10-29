# newbox-baseline

**Problem.** The new VPS (64.177.49.68, installed 2026-10-12) runs the full
Jerusalem 1.0 manager queue at repo HEAD, but the install only covered the
application layer: manager + service + Docker + ffmpeg + hourly log trimmer.
The host-level operational baseline the old box has — the minute-cron
`df-watchdog.sh` (auto-recovery), the daily state backup, Docker log rotation —
was never set up there. A fleet created on the new box would run with no
auto-recovery and no backups.

**Fix.** `baseline-newbox.sh`: one idempotent root script that installs and
verifies the whole baseline in order (watchdog + minute cron, Docker log
rotation via semantic `daemon.json` merge, binder count surface, backup
scripts + daily cron, backup run + integrity drill), aborting on the first
hard failure with a per-step summary. Re-running is always safe.

**Design notes.**

- Sources are explicit argv paths (`bash -n` gated, sha256-compared, `.prev`
  kept on replace) — the same pattern as the `push-*.sh` scripts; nothing is
  fetched from the network and instance data/volumes/containers are never
  touched.
- Cron lives in `/etc/cron.d/jerusalem-watchdog` + `/etc/cron.d/jerusalem-backup`
  (exact-content files), but defers to root's personal crontab when it already
  covers the job — a user crontab is never clobbered.
- `daemon.json` is merged semantically with python3 (existing keys preserved);
  invalid JSON fails closed with a manual recipe instead of being overwritten.
  `systemctl reload docker` runs only when the file actually changed
  (reload never stops containers; failure is warn-only).
- The backup drill reuses the real `backup-state.sh` / `verify-backup.sh`
  (which already log names/counts only), so the drill proves the cron line
  works without ever printing secrets.
- Hermetic env overrides (`NB_CRON_D`, `NB_BINDIR`, `NB_DOCKER_JSON`,
  `NB_JDIR`, `NB_BINDER_GLOB`, `NB_MIN_BINDER`) keep the 53-test suite off the
  real filesystem.

**Tests.** `test_newbox_baseline.sh` — 53/53 hermetic: arg/source validation,
`bash -n` gate, non-root/docker-missing refusal, fresh-install happy path
(all 9 steps, byte-identical installs, exact cron lines, drill pass, no secret
in output), idempotent re-run (no second docker reload, no spurious `.prev`),
changed-source `.prev` semantics, daemon.json merge/preserve/already-correct/
invalid fail-closed, reload-failure warn-only, binder thresholds, personal-
crontab deference, drill-failure abort, inactive-service warn. The suite caught
one real product bug before commit: `ensure_cronfile`'s single-line
`local name="$1" ... f="$CRON_D/$name"` expanded `$name` from the outer scope
under `set -u` (same bug class as the v6 `note_event` fix) — now split across
two `local` lines.

**Status.** Built + tested 2026-10-13 ~03:50 UTC; wired as apply-all preflight
t50 (49 suites). Repo-side package (no staged files — the new box is not the
apply-all target). Run once by hand on the new VPS per DEPLOY.md.
