# login-pwd-cleanup

## The bug

`_roblox_login_inner()` staged the local Roblox password as a cleartext
`pwd.txt` in the instance volume with a bare `open("wb")` (default
perms) and removed it only on the happy path. Two leak paths:

1. The `auto_login.sh` write failed -> the function returned
   "could not write login script" WITHOUT removing `pwd.txt`.
2. A manager crash (kill -9, power loss) between the `pwd.txt` write
   and its removal left the cleartext password in the volume forever —
   readable from the host volume path and from inside Android — and
   nothing ever cleaned it up.

The remote path already had a 60s delayed delete for its staged
`input-b64.txt` (agent 1.0.38); the local path had nothing.

## The fix (manager/app.py only)

- `pwd.txt` is written atomically (tmp+rename) with 600 perms.
- `_discard_pwd_file()` removes `pwd.txt` + `pwd.txt.tmp`,
  best-effort, never raises, on EVERY exit path: happy path
  (`finally`), a raising `waydroid_exec` (`finally`), and a failed
  script write.
- `sweep_stale_pwd_files()` removes leftovers from local instance
  volumes; `main()` runs it at startup, before any login thread can
  start, so a manager restart — the recovery path from exactly the
  crash in (2) — always cleans up. Remote instances are skipped
  (their staging lives on the node); names are gated with
  `valid_instance_name()` so a hand-edited `instances.json` can never
  turn the sweep into a path-traversal delete. The sweep prints a
  line only when it actually removes something.

## Known limitation

A crash leaves the window between the write and the next manager
restart; the file is 600 on the host in that window, but inside
Android the container runs as root, so 600 is a host-side barrier
only. The sweep bounds the exposure to "until the next manager
restart" instead of "forever". The remote `input-b64.txt` staging has
the same in-container property (60s delayed delete); fixing that
needs an agent-side startup sweep and a version bump, left for a
later package to avoid a cascade in this one.

## Tests

`test_login_pwd_cleanup.py`, 32/32: sweep removal/counts, remote
skip, traversal guard, never-raises paths, happy-path staging
(600 + exact content during exec, gone after), raising-exec finally
proof, script-write-failure regression proof (old code leaked),
pwd-write failure, and source contracts (main() wiring, no bare
write remains, tmp+rename+600, finally discard).
