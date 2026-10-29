# DEPLOY.md — wing-installer-staging

Stages the two wing node installers (`wing/install-wing.sh`,
`wing/install-wing.bat`) in the apply-all code queue so installer
fixes actually reach the VPS. Rides apply-all from HEAD — no
standalone scp.

## Why

The manager serves all three wing downloads from `WING_DIR` on disk
(`<DATA_DIR>/wing/` = `/root/jerusalem/wing/` on the VPS) at
`/api/install/<name>` with the `__MAIN_URL__` / `__JOIN_TOKEN__`
placeholders replaced at serve time (no restart needed — files are
read per request). But apply-all only ever staged `wing/agent.py`:
`install-wing.sh` and `install-wing.bat` had no deploy path. The
marker-verified `agent.py` download fix in `install-wing.sh`
(wing-installer-agent-check) would have stayed local forever without
a hand-rolled scp.

## What changes

- `deploy-all.sh`: `SOURCES`/`STAGE_NAMES` gain
  `wing/install-wing.sh wing/install-wing.bat` (19 staged files now),
  preflight t90, header/green-log/APPLY.md updated.
- `vps-apply.sh`: `KEYS` gain both files; `target_for` maps them to
  `$DF_DIR/wing/install-wing.sh` / `.bat`; `syntax_check` runs
  `bash -n` on the deployed `.sh` (the `.bat` is not shell — never
  bash -n'd). Install is a plain `cp -f` (backup + automatic rollback
  still apply); placeholders stay intact, replacement happens at
  serve time.
- `verify-live.sh --ssh`: probes both files against repo HEAD
  (queue-audit check 8 covers this wiring).

## Safety

- Installers are served from disk per request — a bad file degrades
  to new-node enrollment only; the running fleet and dashboard are
  untouched.
- Token hygiene is test-enforced: every `JOIN_TOKEN=` assignment in
  both installers must be exactly the `__JOIN_TOKEN__` placeholder
  (a hardcoded real token fails the suite — the 2026-10-06 security
  bug class).
- No restart dependency: the manager picks up the new installers
  on the next `/api/install/` request; the apply-all restart happens
  anyway for the other staged files.

## Deploy

From an SSH-capable session:

```bash
bash pending-deploy/apply-all/deploy-all.sh            # old VPS (default target)
# or
bash pending-deploy/newbox-sync/sync-newbox.sh --yes   # new VPS 64.177.49.68
bash /root/jerusalem/finish-deploy.sh                  # on the VPS, after apply-all
```

Verify from the repo machine:

```bash
bash pending-deploy/live-verify/verify-live.sh --ssh   # expect [OK] for both installers
```

No manual steps beyond the standard queue flow.
