# post-deploy — one-command post-apply-all runner (`finish-deploy.sh`)

## Problem

The deploy queue is ~46 suites / 12 staged files deep, and every optimizer
session appends more "run by hand once on the VPS" steps. The next SSH-capable
session's list had grown to a fragile 6-step manual sequence:

1. `bash /root/jerusalem/push-keepalive.sh` (keepalive v2 into live containers)
2. `bash /root/jerusalem/push-booted-watcher.sh` (booted-watcher into live containers)
3. `bash /root/jerusalem/push-screencap.sh` (adaptive screencap loop into live containers)
4. `bash /root/jerusalem/repoint-backup-cron.sh` (backup cron repoint)
5. Replace the 03:17 backup cron line with `backup-state.sh` (hand-edited crontab)
6. Run `backup-state.sh` once + drill the archive with `verify-backup.sh`

Run out of order (or half-forgotten), the box ends up half-migrated: new
manager code talking to old in-container loops, or the new backup script
staged but the old explicit-list cron still running.

## Solution

`finish-deploy.sh` (staged to `/root/jerusalem/finish-deploy.sh` by
apply-all, **not** executed by `vps-apply.sh` — same manual-run pattern as
the push scripts) runs all seven steps in order on the main VPS as root:

- **Preflight first**: requires all 18 staged files at their real on-VPS
  paths (mirroring `vps-apply.sh target_for` — e.g. `wing/agent.py` at
  `/root/jerusalem/wing/`, `index.html` at `/root/jerusalem/static/`)
  — a partial stage can never reach the live containers half-applied.
- **Abort on first failure** with a per-step summary; every step is
  idempotent, so re-running is always safe.
- **Push steps** (1–4): `push-keepalive.sh`, `push-booted-watcher.sh`,
  `push-screencap.sh`, `push-optimizer.sh` into every booted `wd-*`
  container. Queue-audit check 9 guarantees every script the orchestrator
  invokes is actually staged (this caught steps 1–2 referencing push
  scripts that apply-all never staged).
- **Cron-line switch** (step 6): replaces legacy `*backup*` cron lines
  (case-insensitive) with the canonical backup-state.sh line, in root's
  personal crontab AND every `/etc/cron.d` file — the same coverage as
  `repoint-backup-cron.sh` (step 5). Without the cron.d half, a box whose
  backup cron lives in `/etc/cron.d` (e.g. installed by
  `baseline-newbox.sh` on a fresh host) aborted this whole run at step 6.
  The canonical line is written per-location: no user field in a personal
  crontab (`17 3 * * * bash …`), the `root` user field in cron.d files
  (`17 3 * * * root bash …`) — a 6-field line in a personal crontab would
  try to run `root` as a command and silently never back up. No-op when
  already switched anywhere; **fails closed** when no backup cron exists
  anywhere (a backup command is never fabricated — same principle as
  `repoint-backup-cron.sh`); unreadable cron.d files also fail closed
  rather than switching blind. Cron `$(...)` sequences are never executed
  (process substitution, no eval/heredoc — the bug class the repoint script
  already fixed once). Legacy lines in several locations are all switched
  with a warning to de-duplicate so the backup fires once.
- **Backup run + drill** (step 7): runs `backup-state.sh` once, then
  `verify-backup.sh` on the newest archive — proves the new cron line works
  before the operator walks away.
- Prints the next step: `verify-live.sh --ssh` from the repo machine.
- Logs carry step names/counts only — never file contents or secrets.

Exit codes: 0 = all steps ok; 1 = a step failed (the summary names it).

## Companion change

`pending-deploy/adaptive-screencap/push-screencap.sh` gained two
backward-compatible env overrides (defaults unchanged):
`PUSH_SCREENCAP_SRC` (default `/root/jerusalem/screencap-loop.sh`) and
`PUSH_SCREENCAP_VOLBASE` (default `/var/lib/docker/volumes`). The
orchestrator passes the staged source explicitly; the overrides also make
the push script hermetically testable.

## Tests

`test_post_deploy.sh` — 84/84 hermetic checks (fake docker, fake crontab,
fake cron.d, fake volumes/state; the REAL staged push/repoint/backup/verify
scripts):

- Happy path: exit 0, all 7 summary steps `[ok]`, crontab switched (legacy
  line gone, exactly one canonical line), archive produced + drilled,
  steps in order, next-step pointer, idempotent re-run.
- Preflight: missing staged file (named, points at deploy-all.sh, no step
  runs), docker missing, crontab binary missing (prints the manual cron
  recipe), non-root refused, missing staged push script.
- Step failure aborts: failing step 1 / optimizer step → exit 1, `[FAIL]`
  in summary, no later step runs.
- Cron switch: already-switched is a byte-identical no-op; no backup line
  anywhere → fail closed without touching anything; cron `$(...)` is never
  executed (regression test); canonical in /etc/cron.d with a clean
  personal crontab → no-op (the new-box case); legacy line in a cron.d
  file → replaced in place, other lines preserved, personal crontab
  untouched; personal-crontab replacement is 5-field (no `root` user
  field); legacy lines in both locations → both switched + multi-location
  warning; canonical in personal + legacy in cron.d → only cron.d
  switched; unreadable cron.d file → fail closed naming the file.
- Backup/drill: backup failure → abort before verify; drill failure → abort.

## Deploy

Staged only — no VPS SSH needed for this item itself. Wiring (done):

- `deploy-all.sh`: SOURCES/STAGE_NAMES + preflight t48 (`84 passed, 0 failed`)
  + bash -n syntax gate + header comment + green-log line.
- `vps-apply.sh`: KEYS allowlist + `/root/jerusalem/finish-deploy.sh`
  destination + bash -n gate.
- `test_apply_all.sh`: fake stage + deployed assertion.
- `APPLY.md`: package table row + suite lists (53 suites).

After `deploy-all.sh` runs on the VPS, the operator runs **one** command:

```bash
bash /root/jerusalem/finish-deploy.sh
```

then, from the repo machine:

```bash
bash pending-deploy/live-verify/verify-live.sh --ssh
```
