# waydroid-exec-cleanup

Follow-up to login-pwd-cleanup + login-script-cleanup, closing the
last staging file in the local login/volume path.

## The gap

`waydroid_exec()` — behind every local Android command (boot checks,
package checks, the login script exec, the `do_instance_input` FIFO
fallback) — wrote its script to a SHARED `cmd.sh` in the instance
volume on every call and NEVER removed it. It held the most recent
command forever: viewer-typed text via the input fallback (whatever
the user typed into a viewer, including typed passwords), the login
script invocation, and every other `android_cmd`. Readable from inside
Android and from the host volume path. No other code reads `cmd.sh`
(grep proves it: only `waydroid_exec` writes and executes it). A
second latent bug rode along: two concurrent `waydroid_exec` calls on
one instance clobbered each other's shared script, so one call could
execute the other's command.

## The fix (manager/app.py only)

- `waydroid_exec()` stages a UNIQUE per-call script
  (`cmd-<hex>.sh` via `secrets.token_hex`) instead of the shared
  `cmd.sh`, executes it, and removes it afterwards on EVERY path:
  happy path, setup failure (partial write removed), exec failure /
  timeout (`finally` block).
- `sweep_stale_pwd_files()` (run by `main()` at startup, before any
  thread starts) now also removes stale `cmd.sh` / `cmd-*.sh` files
  from local instance volumes, covering the one path the post-run rm
  cannot: a manager crash between the write and the rm. The legacy
  shared `cmd.sh` is covered too, so pre-fix volumes get cleaned on
  the next restart after upgrade.
- Sweep rules unchanged: remote instances skipped, names gated
  against traversal, flat dir listing with name-gated patterns only,
  unrelated files untouched (`cmd.txt`, `xcmd-*.sh`, bare `cmd-` all
  survive — tested).

## Tests

33/33 hermetic checks (`test_waydroid_exec_cleanup.py`, t124):
unique-name staging + same-name exec + post-run rm, setup-failure /
nonzero-rc / raising-exec cleanup, invalid/metachar name gates, full
login regression (staged content runs `auto_login.sh`, no `cmd*.sh`
left after login), sweep coverage + unrelated-file safety + remote /
traversal / missing-dir guards, source contracts. The suite fails
against the pre-fix `app.py` (shared `cmd.sh`, no rm anywhere) —
verified by loading the HEAD version.

Cross-suite fallout, found by the full `--stage-only` dry run:
`route-name-gate` (t82) asserted exactly 2 shell calls for a valid
`waydroid_exec` (setup + exec) — the post-run rm is legitimately a
third, so its expectation is now setup + exec + rm (still 35/35, and
the rm call must name a `cmd-` script). No other suite drives the
real `waydroid_exec` through fake shell calls.

## Deploy

Rides apply-all from HEAD (`manager/app.py` already staged).
`pending-deploy/remote-autologin/app.py` snapshot refreshed to HEAD
(audit check 4). See DEPLOY.md.
