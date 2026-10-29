# boot-complete — DEPLOY

## Problem

`entrypoint.sh` waits at most 5 minutes for `sys.boot_completed`. On a slow
first boot (fleet creation under load — exactly the new-box srt1–srt5
situation), that window closes with the entire "booted" block skipped:

- no Roblox install from the baked-in split APKs,
- no canonical helper scripts (`screencap-loop.sh`, `roblox-keepalive.sh`,
  `android-optimize.sh`) copied into the data volume,
- no screencap loop / input daemon / keepalive started,
- no Android optimizer run, no Roblox launch.

`booted-watcher.sh` later writes the `booted` marker, so the dashboard stops
saying "booting" — but the instance is a shell. The host watchdog's
helper-restart (df-watchdog.sh section 3) then runs
`waydroid shell sh /data/media/0/jerusalem/<helper>.sh` on files that were
never installed: silent failure, logged as "restart", retried until the
helper circuit breaker (>8/10min) trips a misleading DEGRADED. Same for the
stale-stream restart (section 4).

## What it does

- **New `waydroid-image/boot-complete.sh`**: the full boot-completion
  sequence, extracted verbatim from `entrypoint.sh` (Roblox install,
  canonical script installs with legacy inline fallbacks, input daemon,
  pgrep-guarded helper starts, optimizer, Roblox launch, `booted` +
  `roblox_done` markers). Idempotent, never deletes, never fails hard.
- **`entrypoint.sh`**: the `BOOTED=1` fast path now delegates to
  `/opt/boot-complete.sh` (WARN + marker-only fallback if the image
  predates it).
- **`waydroid-image/Dockerfile`**: `COPY boot-complete.sh
  /opt/boot-complete.sh`.
- **`booted-watcher.sh`**: when it observes a *late* boot (marker missing,
  `sys.boot_completed=1`), it runs `/opt/boot-complete.sh` before writing
  the marker. Old images without the file keep the pure-surfacing behavior.
  Hook failures still write the marker (with a WARN) — surfacing never
  breaks.
- **`df-watchdog.sh` (sections 3+4)**: if a helper script is missing from
  the data volume, log one distinct WARN per day naming the real cause
  ("boot setup never installed it") and do NOT attempt the restart or burn
  the circuit-breaker budget on a certain failure.

## Tests

`test_boot_complete.sh` — 63/63 pass (hermetic):

- **A (boot-complete.sh vs fake waydroid/pgrep)**: fresh install (pm
  install of both splits, markers, script installs, helper starts,
  optimizer, launch taps), already-installed skip, install failure and
  missing APKs (WARN, no crash, exit 0), missing /opt scripts (WARN +
  legacy inline fallbacks), pgrep guard (no duplicate starts),
  idempotent re-run, totally-dead waydroid (exit 0).
- **B (wiring)**: entrypoint delegates, old inline sequence gone,
  Dockerfile COPY present, every literal `/opt/*.sh` ref and every
  `$OPT/*` file has a COPY line.
- **C (booted-watcher hook)**: late boot runs the hook; missing hook
  script keeps legacy touch behavior; marker-present never re-runs the
  hook; hook failure still writes the marker with a WARN.
- **D (watchdog, real script vs fake docker)**: helper dead + script
  missing → distinct WARN, no restart, no budget burn, no DEGRADED;
  script present → restart as before (regression); stale stream + missing
  loop script → WARN, no pkill; WARN deduped to one line per day.

Also updated for the refactor (counts changed, all green):
`android-optimizer` test (45/45 — now asserts on boot-complete.sh +
entrypoint delegation) and `adaptive-screencap` test (43/43 — same).

## Deploy

Baked into the Docker image, not scp'd to `/root/jerusalem`:

1. The next `waydroid-instance` image build picks it up automatically
   (`vps-image-build.sh` fail-closes on an incomplete context; the
   Dockerfile COPY is verified by test B3/B5).
2. `df-watchdog.sh` (with the sections 3+4 guards) rides
   `pending-deploy/apply-all/deploy-all.sh` from HEAD (already in the
   stage list → `/usr/local/bin/df-watchdog.sh`, no restart — the
   minute-cron picks it up).
3. No live-container push needed: the missing-script case it guards
   against only arises on images predating `boot-complete.sh`, and those
   containers get the full setup from the existing `push-*.sh` scripts
   via `finish-deploy.sh`.

## Verify

- `bash pending-deploy/boot-complete/test_boot_complete.sh` → `63 passed, 0 failed`
- `bash -n` clean on all four touched scripts (in apply-all preflight syntax gate)
- Full `--stage-only` dry run green (87 suites)
- On the next image build: `docker run --rm --entrypoint sh
  waydroid-instance:latest -c 'ls -la /opt/boot-complete.sh'` lists the file
