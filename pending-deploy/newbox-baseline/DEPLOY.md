# DEPLOY — newbox-baseline

One-command host-level baseline for the new VPS (64.177.49.68). The 2026-10-12
install put the manager, service, Docker, ffmpeg and the hourly log trimmer on
the new box, but NOT the minute-cron watchdog, the daily state backup, or
Docker log rotation. Run this once (as root) before the fleet lands on the new
box; re-running later is always safe (idempotent).

## Morning SSH session

```bash
# 1. from ~/workspace/droidfarm on the repo machine — stage the 4 files:
scp waydroid-image/df-watchdog.sh \
    pending-deploy/backup-state/backup-state.sh \
    pending-deploy/backup-state/verify-backup.sh \
    pending-deploy/newbox-baseline/baseline-newbox.sh \
    root@64.177.49.68:/root/

# 2. on the VPS as root — one command:
bash /root/baseline-newbox.sh /root/df-watchdog.sh /root/backup-state.sh /root/verify-backup.sh
```

Exit 0 = all steps ok (warnings listed, non-fatal). Exit 1 = a step failed;
the summary names it — fix and re-run (idempotent, resumes cleanly).

## What it does (in order, aborts on first hard failure)

1. Preflight: root, `docker` + `python3` present; `jerusalem.service` state
   reported (warn-only).
2. Installs `/usr/local/bin/df-watchdog.sh` from the staged source
   (`bash -n` gate; installs only on sha256 difference; replaced target kept
   as `.prev`).
3. Minute cron `/etc/cron.d/jerusalem-watchdog`
   (`* * * * * root /usr/local/bin/df-watchdog.sh`) — skipped with a note when
   root's personal crontab already covers df-watchdog (never clobbers it).
4. `/etc/docker/daemon.json` merged (python3, semantic compare) to
   `log-driver: json-file` + `log-opts: {max-size: 10m, max-file: 3}` — the
   old box's rotation; existing keys preserved; invalid JSON fails closed
   (never clobbered). `systemctl reload docker` only when the file changed
   (warn-only on failure; containers are unaffected by reload).
5. Binder device count surfaced (warn when < 60; the manager self-heals the
   pool at instance-create time — this only makes it visible).
6. Installs `backup-state.sh` + `verify-backup.sh` into `/root/jerusalem/`
   (same `bash -n` + sha256 + `.prev` rules).
7. Daily cron `/etc/cron.d/jerusalem-backup`
   (`17 3 * * * root bash /root/jerusalem/backup-state.sh`) — same
   personal-crontab deference as step 3.
8. Backup drill: runs `backup-state.sh` once, then `verify-backup.sh` on the
   newest archive (proves the new cron line works end to end).
9. Summary + `verify-live.sh --ssh` pointer.

## Verify (from the repo machine)

```bash
bash pending-deploy/live-verify/verify-live.sh --host=64.177.49.68 --ssh
```

Expect: 19 staged files match HEAD, `df-watchdog minute-cron installed`,
backup cron in canonical form (`backup-state.sh`), binder count ≥ 60.

## After this

- `pending-deploy/waydroid-image-build/DEPLOY.md` — build `waydroid-instance:latest`
  on the new box (needs the staged Roblox APKs).
- Fleet creation itself is Penta's call (the account mapping
  `roblox_accounts.json` lives on the old box's state dir).
- This package is repo-side only (like `waydroid-image-build`): it does NOT
  ride `apply-all` (which targets the old VPS 209.222.10.93) and stages no
  files, so `vps-apply.sh` KEYS and the `--ssh` probe list are untouched.
