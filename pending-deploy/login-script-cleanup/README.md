# login-script-cleanup

## The bug

`_roblox_login_inner()` staged the local Roblox password as `pwd.txt`
and the login script as `auto_login.sh` in the instance volume. The
login-pwd-cleanup session fixed the `pwd.txt` half (atomic 600
staging, discard on every exit path, startup sweep) but the script
half was left behind: `auto_login.sh` was written on every local
login and NEVER removed — not on any exit path, not by the startup
sweep, not by anything else (it has no other reader anywhere in the
codebase). The script carries the account username (shlex-quoted)
plus the verified login tap coordinates, world-readable by default,
readable from inside Android and from the host volume path,
persisting forever after every login. Same leak class as the
password file, smaller secret.

## The fix (manager/app.py only)

- `_discard_login_script()` removes `auto_login.sh`, best-effort,
  never raises, on EVERY exit path: happy path (`finally`), a
  raising `waydroid_exec` (`finally`), and a failed script write.
- `sweep_stale_pwd_files()` (name kept from the pwd.txt-only era)
  now also removes `auto_login.sh` leftovers from local instance
  volumes; `main()` runs it at startup, before any login thread can
  start, so a manager restart — the recovery path from exactly the
  crash case — always cleans up. Remote instances are skipped
  (their staging lives on the node); names are gated with
  `valid_instance_name()` so a hand-edited `instances.json` can
  never turn the sweep into a path-traversal delete. The sweep
  prints a line only when it actually removes something.

## Known limitation

Same as login-pwd-cleanup: a crash leaves the window between the
write and the next manager restart; inside Android the container
runs as root, so file perms are a host-side barrier only. The
sweep bounds the exposure to "until the next manager restart"
instead of "forever". The script write itself is still a bare
`open("w")` (not atomic, default perms) — deliberate: the script
is only ever executed by the immediately following `waydroid_exec`,
so a torn write from a crash is never executed, and changing the
write protocol would churn the login-username-escape harness for no
exposure reduction (the file is deleted seconds later either way).

## Tests

`test_login_script_cleanup.py`, 25/25: sweep removal/counts
(script alone, all three files), remote skip, traversal guard,
missing-dir and is-a-directory never-raise paths, discard helper
(remove/missing/dir), happy-path script present during exec with
the username line and gone after (old code left it), raising-exec
finally proof, script-write-failure path, and source contracts
(helper never-raises shape, finally discards both files,
script-write-failure discards the script, sweep covers the script,
main() wiring, no other readers).
