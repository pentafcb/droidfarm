# DEPLOY: state-load-robustness

## What

One layer sideways from t152: t152 fixed the SHAPE of state files (a
valid JSON list/string/number/null coerced to {}), but the five
manager state loaders still caught only (FileNotFoundError,
json.JSONDecodeError). Two unreadable-file classes escaped that tuple
and killed the handler thread with no response on every caller:

1. `UnicodeDecodeError` — a state file with invalid UTF-8 bytes (disk
   corruption, bad hand edit). `json.load()` raises it, and it is NOT
   a JSONDecodeError, so the old except missed it.
2. `OSError` beyond FileNotFoundError — a state path that is a
   directory (IsADirectoryError), unreadable (PermissionError), or an
   I/O error. None of these is FileNotFoundError, so the old except
   missed them too.

Blast radius: `load_providers` / `load_instances` / `load_accounts`
feed the dashboard's /api/instances poll and the public
/api/providers/register route (which does `providers.items()`);
`load_tokens` is called unguarded by `main()`, so a bad-bytes tokens
file killed the manager AT STARTUP into a systemd restart loop — a
total outage, where a missing tokens file simply means "no sessions".

Now (manager/app.py only, no agent change, no dashboard change, no wire
protocol change): all five loaders catch `(OSError, ValueError)`. That
tuple covers the old classes (FileNotFoundError is an OSError,
JSONDecodeError is a ValueError) plus both new ones (UnicodeDecodeError
is a ValueError, IsADirectoryError / PermissionError are OSErrors), so
an unreadable state file degrades to "no state" exactly like a missing
file. Valid dict files load byte-identically.
The wing agent is untouched (still 1.0.45), so no VERSION bump and no
agent self-update rides this package.
Remote-autologin app.py snapshot refreshed to HEAD.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py`, so no new stage files. The preflight runs
`pending-deploy/state-load-robustness/test_state_load_robustness.py`
as t153 (exact-count `PASS=40 FAIL=0`).

Standalone fallback (SSH-capable session only): copy
`manager/app.py` to `/root/jerusalem/app.py` on the VPS, then
`systemctl restart jerusalem` and verify with `systemctl is-active
jerusalem` plus a dashboard load. After deploy, a state file replaced
by bad bytes or a directory degrades to "no state" instead of dropping
requests, and a corrupt tokens file no longer prevents manager
startup.

## Verify

- `python3 pending-deploy/state-load-robustness/test_state_load_robustness.py`
  prints `PASS=40 FAIL=0` (regression proof, real loaders over
  bad-bytes / directory / valid / missing / malformed files for all
  five loaders, load_tokens filtering, source contracts + snapshot
  parity).
- Full `--stage-only` dry run green (queue-audit 0 findings, all
  suites green, py_compile + bash -n + JS gate clean).
