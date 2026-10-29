# remote-b64-sweep

Follow-up to remote-input-b64 + the manager-side staging cleanups
(login-pwd-cleanup, login-script-cleanup, waydroid-exec-cleanup),
closing the last staging file with no crash-window coverage: the
REMOTE b64 text file.

## The gap

`_stage_b64_input_text()` (wing agent, 1.0.38) stages a decoded b64
`/input` payload in the instance's data volume
(`.../data/media/0/jerusalem/input-b64.txt`, 600, atomic tmp+rename)
and sends `input text "$(cat ...)"` through the FIFO. The staged file
is deleted ~60s later by a daemon thread, and only when its content
still matches what was staged. That thread is the ONLY cleanup, and
it dies with the agent process:

- Agent restart inside the 60s window (crash, self-update restart,
  systemd restart, host reboot) -> the file stays forever. The new
  process has no thread for it and no sweep ever looked for it:
  `main()` had no staging sweep, and the selfwatch pass only swept
  `stream_wanted`.
- Thread-start failure -> the handler 500s, but the file is already
  staged and no thread exists to delete it.
- Crash between the tmp write and `os.replace` -> `input-b64.txt.tmp`
  (same secret bytes) stays behind; the except-path rm only runs on
  a caught exception, not on a kill.

The exposure matches the local pwd.txt class exactly: readable from
inside Android (the volume is the instance's media dir) and from the
host volume path, and it is usually a Roblox login password (the
manager sends the remote login password with `b64: true` precisely
because it contains characters the sanitize path strips).

## The fix (wing/agent.py only)

- `sweep_stale_b64_input_text(names, vol_base, max_age_s)`:
  per-instance removal of `input-b64.txt` + `input-b64.txt.tmp`.
  Names come from `load_instances()` by default (dict keys, or any
  iterable); paths come from `_agent_input_b64_path`, so traversal
  names are refused by the same gate the staging path uses. Returns
  the removal count. Never raises: per-file OSError is swallowed,
  a bad instances value yields 0, an unreadable mtime under an age
  gate skips that file instead of deleting blind.
- `main()` calls it unconditionally right after the self-update
  rollback guard, before any supervised thread starts. Ordering
  matters: at that point the HTTP server is not bound, so no `/input`
  can be in flight and no live stage can exist — unconditional
  removal is safe.
- `selfwatch_check_instance()` section 0b (next to the section 0
  `stream_wanted` sweep) calls it with `max_age_s = 2 * _B64_TEXT_TTL_S`
  (120s) and logs removals to the selfwatch log. The age bound is
  what makes the periodic sweep safe: a live stage is deleted by its
  own thread at 60s, so a file older than 120s is orphaned by
  definition, and its FIFO eval happened within milliseconds of
  staging — deleting at 120s+ cannot break a live typing (the
  existing 60s thread already assumes eval within 60s). Alert-only:
  like the section 0 sweep, it never touches `healthy` or any
  recovery budget.

## Why no version bump

No manager gate depends on the sweep (the login gate is >= 1.0.38,
still satisfied), and node self-update is sha256-driven, so the fix
ships on the next heartbeat after the manager deploy — the same
precedent as remote-launch-fifo and agent-ip-gate. A bump would
cascade through the VERSION-membership suites for zero gate benefit.

## Tests

`test_remote_b64_sweep.py` — 42 checks: version still 1.0.38, path
gating (traversal/metachar/empty/oversized names), startup sweep
(file + tmp, no-op second sweep, missing dir, unrelated files
untouched, traversal skip, load_instances default, dict/list/bad
instances values, is-a-directory never-raises), age-gated sweep
(fresh survives, 3600s-old removed, fresh/old tmp, larger gate,
missing instance), real selfwatch passes via a fake docker (stale
removed + healthy + log line; fresh survives + healthy + no log
line), crash-window proof (a thread-less staged file persists until
the sweep removes it), staging regression (cat form, byte-identical
secret, TTL thread still deletes), and source contracts (main()
order: rollback guard -> sweep -> serve_forever; selfwatch section
placement before the container check; 2x TTL gate; _sw_log line;
tmp sibling covered).

## Deploy

Rides apply-all from HEAD (`wing/agent.py` already staged).
`pending-deploy/remote-autologin/agent.py` snapshot refreshed to HEAD
(audit check 4). Docs: this package's DEPLOY.md; APPLY.md table row +
suite lists updated (now 125 suites).
