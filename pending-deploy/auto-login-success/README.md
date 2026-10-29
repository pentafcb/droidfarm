# auto-login-success — auto-login watcher stops after a success

## Problem

`auto_login_watcher()` fired a Roblox login sequence on an instance's
first boot up to 3 times (its documented "max 3 attempts" cap) but
NEVER checked whether an attempt had already succeeded: it never read
`roblox_login_results.json`. So a first boot ALWAYS burned all 3
attempts even when attempt 1 logged in fine — attempts 2 and 3 then
re-tapped the username field (512,228), password field (512,296) and
Sign In (512,367) coordinates into whatever screen Roblox was showing
post-login (game UI, not the login form). A second, related gap: the
watcher loop runs every 60s but a login sequence can take longer
(waydroid_exec timeouts), so a second sequence could start while the
first was still tapping, interleaving two tap streams.

## Fix (manager/app.py only)

- Success short-circuit: before firing, the pass checks the recorded
  login result for the instance; ok=True with ts >= this boot's start
  marks `login_boot = started` and fires nothing. A result from an
  older boot always has ts < started, so it never short-circuits a
  fresh boot. Manual/batch successes count too — logged in is logged
  in.
- In-flight guard: `_auto_login_inflight` (set, `_lock`-guarded) holds
  names with a sequence running; the pass skips those without burning
  an attempt. `_auto_login_run()` discards its name in a `finally`
  block (success, failure, or exception), and a `Thread.start()`
  failure also releases the guard, so no path can wedge a boot.
- Testability: the pass body moved to `_auto_login_pass()` (same code,
  same never-raises contract); `auto_login_watcher()` is now just
  sleep(90) + loop { `_auto_login_pass()`; sleep(60) }.

## Tests

`test_auto_login_success.py` — 37/37 hermetic (real app import, all
docker/waydroid/network deps faked, spawned threads faked):
success/manual/batch short-circuit, older-boot success and failed
results still firing, malformed results never raising, in-flight skip
without burning an attempt, guard lifecycle incl. exception run and
start-failure release, pre-existing bounds unchanged (attempts
exhausted, restart window, container not started), remote short-circuit
skipping the wing status call, pass never raising, source contracts.
