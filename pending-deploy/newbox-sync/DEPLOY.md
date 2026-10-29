# newbox-sync — one-command code+baseline sync for the NEW VPS

## Problem
The new VPS (64.177.49.68) runs the full Jerusalem 1.0 queue but is NOT the
default `apply-all` target (`deploy-all.sh` defaults to the old VPS
209.222.10.93). Every new `app.py` / `index.html` / `agent.py` change would
otherwise need a hand-rolled `scp` to the new box — the morning SSH session's
new-box list had grown to 4+ separate manual commands (app.py sync for the
binderfs modprobe fix, baseline-newbox.sh, verify, …). Run out of order or
half-forgotten, the box ends up half-migrated.

## What it does
`sync-newbox.sh [user@host] [--yes]` (host defaults to `root@64.177.49.68`),
three steps in order, aborting on the first failure with a per-step summary:

1. **Code sync** — `deploy-all.sh <host> --yes`: the FULL preflight (queue
   audit, all unit suites, dashboard JS gate, syntax), stage build from repo
   HEAD, `scp`, then the host-agnostic `vps-apply.sh` on the box (timestamped
   backup → install → `py_compile`/`bash -n` gate → single
   `systemctl restart jerusalem` → verify → automatic rollback on failure).
   This is what carries the binderfs `modprobe` self-heal that unblocks
   srt1–srt5 creation, plus every other staged queue item.
2. **Baseline** — `scp`s `baseline-newbox.sh` + its 3 sources
   (`df-watchdog.sh`, `backup-state.sh`, `verify-backup.sh`) to
   `/root/jerusalem-deploy/newbox-baseline/` and runs it: minute-cron
   watchdog, Docker log rotation, binder device count surface, 03:17 daily
   state backup + integrity drill. Idempotent; installs only on sha256
   difference, so re-running after a code sync is a safe no-op for
   `df-watchdog.sh`.
3. **Verify** — `verify-live.sh --host=<bare host> --ssh`: file-level SHA
   check of all 19 staged files vs repo HEAD + `jerusalem.service` state +
   route probes.

`verify-live.sh --ssh` prepends `root@` itself, so the script strips a
`user@` prefix before passing `--host`.

## Tests
`test_newbox_sync.sh` — 45/45 pass (hermetic: fake deploy-all.sh / scp / ssh
/ verify-live.sh with a call log; failure injection): default + custom host,
`--yes` forwarding and prompt behavior (`n` aborts before anything runs),
exact step order (deploy-all → ssh mkdir → scp → ssh baseline run →
verify-live), abort-on-first-failure for every step (nothing later runs),
per-step summary naming the failed step, bare-host `--host` for verify-live,
the exact 4 baseline sources in the `scp`, baseline invoked with its 3
source args, usage error on unknown flags, no secrets in output, `bash -n`
clean, no legacy refs.

## Deploy (from an SSH-capable session)
```bash
bash ~/workspace/droidfarm/pending-deploy/newbox-sync/sync-newbox.sh --yes
# or, to confirm each step first:
bash ~/workspace/droidfarm/pending-deploy/newbox-sync/sync-newbox.sh
```
Exit 0 = all three steps ok. Exit 1 = a step failed (the summary names it;
code+baseline are already applied at that point — no silent half state).
The script ends by listing what stays manual (see below).

## Verify
- `sync-newbox.sh` exit 0 and its step 3 VERDICT line.
- Dashboard `http://64.177.49.68:8080` → HTTP 200; served `index.html`
  SHA matches repo HEAD.
- `/ws/h264/rbx1` → 401 (route live, H.264 armed).
- On the box: `systemctl is-active jerusalem` → active;
  `/etc/cron.d/jerusalem-watchdog` present; the 03:17 backup cron calls
  `backup-state.sh`.

## Rollback
- Step 1 rolls back automatically via `vps-apply.sh` (timestamped backup in
  `/root/jerusalem/.deploy-backup/`, restore + restart + re-verify).
- Step 2 is idempotent — re-running converges; it never deletes anything
  except replacing files it installed (keeping one `.prev`).
- Step 3 changes nothing.

## Still manual after this (need Penta's decisions / the APK upload)
- `waydroid-image-build`: Roblox APK upload + `docker build` on the new box
  (no fleet can be created without the `waydroid-instance` image).
- `state-migrate`: import the old box's backup archive (account mapping).
- Creating the srt1–srt5 fleet from the dashboard.
- Decommissioning the old VPS (Penta hasn't answered replacement vs second
  host).

## Blocked
VPS SSH is dead in worker sessions (key auth rejected, password never
stored) — run the command above from an SSH-capable session.
