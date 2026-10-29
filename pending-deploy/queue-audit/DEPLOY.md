# queue-audit — deploy-queue wiring auditor

## What it is

`audit-queue.sh` is a mechanical consistency audit for the `pending-deploy/`
queue. The queue's wiring is manually synced across five places — the package
dirs, `apply-all/deploy-all.sh` (preflight suite list, `SOURCES`/`STAGE_NAMES`),
`apply-all/vps-apply.sh` (`KEYS` stage allowlist), `apply-all/APPLY.md`
(docs), and `live-verify/verify-live.sh` (`--ssh` probes every staged file) —
and drift there is silent: a deploy that skips a new package's tests,
ships a stale snapshot copy, stages an unexpected file set, or leaves a
staged file unprobed by the live-verifier. The auditor
fails closed on any mismatch.

It checks:
1. Every package dir (except `apply-all`, `queue-audit`) has a non-empty `DEPLOY.md`.
2. Every `test_*` under `pending-deploy/*/` is invoked in the `deploy-all.sh`
   preflight exactly once — and every invoked test path exists.
   (`apply-all/test_apply_all.sh` is exempt: it tests the orchestrator itself.)
3. `deploy-all.sh` `SOURCES`/`STAGE_NAMES` stage-file set == `vps-apply.sh`
   `KEYS` allowlist, and every `SOURCES` path exists in the repo.
4. `SNAPSHOTS` manifests: staged copies of canonical repo files are
   byte-identical to the canonicals (stale snapshots ship old code).
5. `APPLY.md` names every package; every code-deploying package appears in the
   "What's included" section (`python-security-patch` is deliberately not
   code-deployed and is exempt from that section only).
6. No legacy pre-rename paths/units survive in deploy scripts (`pending-deploy/*/*.sh`)
   or `DEPLOY.md` files. `test_*` files, hyphenated names (e.g. `droidfarm-wing`),
   and lines marked `legacy-guard` are exempt.
7. No unmarked old product-name refs in deploy scripts, `DEPLOY.md` files, or
   `APPLY.md`. `test_*` files, `legacy-guard`-marked lines, and rename-history
   lines are exempt.
8. `live-verify/verify-live.sh --ssh` probes every `STAGE_NAMES` file
   (basename match against the remote `for f in` probe list; `df-watchdog.sh`
   is covered by its dedicated `SHA_watchdog` probe since it installs to
   `/usr/local/bin`, outside `/root/jerusalem`). A staged file the verifier
   never checks is a blind spot — a partial deploy failing on exactly that
   file would still report "LIVE MATCHES HEAD".
9. `post-deploy/finish-deploy.sh` only runs staged scripts: every
   `$DF_DIR/*.sh` it invokes must be a `STAGE_NAMES` entry, and its `STAGED`
   preflight list must cover every staged file (except `finish-deploy.sh`
   itself). Catches the 2026-10-13 incident where steps 1-2 invoked push
   scripts that apply-all never staged.
10. Roblox pin agreement: the fleet's pinned Roblox version is defined in
    four places that must agree — the main-node drift guard
    (`waydroid-image/df-watchdog.sh` `PINNED_ROBLOX` default), the wing
    drift guard (`wing/agent.py` `PINNED_ROBLOX` default), and the
    image-build scripts (`fetch-roblox-apks.sh` documents the pin its
    sha256 pins belong to; `extract-roblox-apks.sh`'s default XAPK path
    names the version). A partial pin bump (drift guards moved but the
    image build still baking the old splits, or vice versa) would flag
    every instance as drifted — or miss real drift. Fails closed when a
    definition is missing or unparsable: a refactored pin line must update
    the check, not slip past it. Second half: the pinned split SHAs
    (`PIN_BASE_SHA_DEFAULT` / `PIN_X86_SHA_DEFAULT`) are hard-coded in both
    `fetch-roblox-apks.sh` and `vps-image-build.sh` (the latter gates the
    morning image build) — the two files must carry identical values, or a
    partial pin bump breaks the build with a confusing mismatch.

## Run it

```bash
# from ~/workspace/droidfarm:
bash pending-deploy/queue-audit/audit-queue.sh
python3 pending-deploy/queue-audit/test_queue_audit.py   # 70/70 hermetic checks
```

It also runs automatically as the first `deploy-all.sh` preflight step, so a
drifted queue can never reach the VPS.

## Adding a new package (checklist the auditor enforces)

1. `pending-deploy/<name>/` with a non-empty `DEPLOY.md` (check 1).
2. Test suite `pending-deploy/<name>/test_*` **and** its invocation in the
   `deploy-all.sh` preflight (exactly once), with a matching result grep
   (check 2). Forget the preflight line and the suite is silently skipped —
   the auditor fails the deploy instead.
3. If the package stages a new file for the VPS: add it to `SOURCES` +
   `STAGE_NAMES` in `deploy-all.sh` **and** to `KEYS` in `vps-apply.sh`
   (check 3 catches a one-sided edit).
4. If the package keeps a snapshot copy of a canonical repo file (e.g. for
   reference/testing): declare it in `pending-deploy/<name>/SNAPSHOTS` as
   `staged/path=canonical/path` (repo-relative), one per line, `#` comments
   allowed — and keep it byte-identical (check 4). The remote-autologin
   `app.py` snapshot drifted 606 lines stale twice before this gate existed.
5. Document the package in `apply-all/APPLY.md`: a row in the "What's included"
   table naming its deployed files (check 5). `python-security-patch` is the
   only package that belongs in the "Deliberately NOT included" section.
6. After any rename of VPS paths, service units, install locations, or the
   product name itself: grep the whole `pending-deploy/` tree for the old
   strings and update every deploy script + `DEPLOY.md` + `APPLY.md`
   (check 6 fails the deploy on leftover paths/units, check 7 on leftover
   product-name refs — e.g. a verify step grepping the log for the old
   banner name). Mark intentional historical mentions `legacy-guard` or
   phrase them as rename history.
7. If the package stages a file for the VPS: add it to the `--ssh` probe
   list in `live-verify/verify-live.sh` (the remote `for f in` loop, or the
   `SHA_watchdog` branch for `/usr/local/bin` installs) — check 8 fails the
   deploy if any staged file goes unprobed.
8. If `finish-deploy.sh` runs your staged script: nothing extra to do — check
   9 verifies the wiring both ways automatically (every `$DF_DIR/*.sh` the
   orchestrator invokes must be staged; its `STAGED` preflight list must
   cover every staged file). If you add a step to `finish-deploy.sh`, extend
   its `STAGED` list and `test_post_deploy.sh` in the same commit.

## Deploy

Nothing to deploy — this package is repo-side only. It rides no stage file;
`deploy-all.sh` runs it from the repo during preflight. No VPS SSH needed.
