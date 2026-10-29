# wing-update-rollback

Wing agent 1.0.29 -> 1.0.30: automatic rollback after a bad self-update.

## The gap

The agent self-update (1.0.24) verifies the download's sha256 and byte-compiles
it before swapping it in — but py_compile only catches syntax errors. A runtime
bug (a module-level NameError, a bad import, a crash inside `main()`) slips
through both checks and crash-loops: under systemd's `Restart=always` (the
installer unit has no StartLimit) AND the `__main__` while-True retry loop.
There is no recovery — the agent IS the node's recovery mechanism (selfwatch,
disk/log guards, heartbeats), so a crash-looping agent is a dead node until a
human intervenes. That is exactly the failure self-update was built to avoid
(Penta re-running installers by hand).

## The fix (`wing/agent.py`)

- `note_agent_update(st_dir, version, from_version)`: a successful update leaves
  a dated marker (`agent.update.marker`, JSON `{version, from, ts}`, atomic
  tmp+rename) and resets the boot log. Called in `maybe_agent_self_update`'s
  success path, before the restart exit.
- `record_agent_boot(st_dir)`: every startup appends one epoch to
  `agent.boot.log`, pruning entries older than `ROLLBACK_WINDOW` (300s).
  Far-future timestamps (clock skew) are dropped; a 1s epsilon tolerates the
  log's own `%.3f` rounding.
- `maybe_rollback_agent_update()`: called at the very top of `main()` so even
  a crash inside `main()`'s body counts toward the storm. A boot storm
  (>= `ROLLBACK_BOOTS` = 4 startups inside 300s) while the marker is fresh
  (<= `ROLLBACK_MARKER_TTL` = 1800s after the update) restores `agent.py.bak`
  — the code that was running minutes ago — after a py_compile check on the
  backup (fail-closed: a bad backup is never swapped in), clears the marker
  and boot log, logs one ALERT (surfaces in the dashboard Eventos modal via
  the existing selfwatch-events path), and re-execs the restored file
  (falls back to `os._exit(1)` so the supervisor restarts into the restored
  file).
- The marker is single-use: it is cleared on every storm outcome (rolled back
  or skipped), so a broken *environment* (not a broken update) can never
  rollback-loop — worst case the node crash-loops on the old code, exactly as
  it would today with no rollback.
- No manager/dashboard changes. The marker is only ever written by the update
  path, so nodes that never self-updated (Windows included) are unaffected.
  All helpers best-effort, never raise.

## Tests

`test_wing_update_rollback.py` — 53/53 hermetic: VERSION exactly 1.0.30 +
parse ordering + manager >=1.0.2 gate; marker JSON shape / atomicity /
bootlog reset / never-raises; boot record append + prune + garbage + future
rejection; marker read (missing/corrupt/expired/non-dict); rollback
(no-marker records boot, ok below threshold, storm -> rolled-back with .bak
restored + marker/bootlog cleared + ALERT + execv argv, storm with missing
.bak -> no-backup, storm with uncompilable .bak -> no-backup, no rollback
loop on the next boot, garbage paths never raise); update path writes the
marker on success; main() wiring (rollback before thread registration).

Preflight t93 (`"PASS=53 FAIL=0"`); 26 existing suites' VERSION tuples widened
to 1.0.30; the exact-bump assertion moves here from wing-missing-script-guard
(1.0.29+ there now), per the handoff pattern; remote-autologin snapshots
refreshed to HEAD (audit check 4).
