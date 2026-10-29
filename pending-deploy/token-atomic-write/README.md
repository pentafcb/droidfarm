# token-atomic-write

Atomic state-file writes for the two manager writers that were still
non-atomic: the static join token (`static_join_token`) and the dashboard
session tokens (`tokens.json`).

## Why
Both used bare `open(path, "w")`. A torn write left a truncated file:
- `static_join_token` truncated -> silent ROTATION of the "static" token on
  next boot (no log line), breaking every saved node-enrollment one-liner
  with unexplained 403s. Penta's standing rule is that the join token stays
  static; a crash must never rotate it silently.
- `tokens.json` truncated -> `load_tokens()` JSONDecodeError -> all
  dashboard sessions logged out on next boot.

## Fix (manager/app.py only)
- `get_static_token()`: tmp+rename write (600 on the tmp before rename);
  loud `[join-token] WARNING` when regenerating over an existing but
  invalid/unreadable file (rotation is never a mystery again); if persist
  fails entirely, serve the in-memory token for this process lifetime
  rather than crash.
- `save_tokens()`: tmp+rename write. Best-effort, never raises (unchanged).

## Tests
`test_token_atomic_write.py` — 25 hermetic checks: fresh generation
(format/persist/600/stability), valid passthrough (byte-identical, mtime
untouched), torn/invalid -> regenerate + loud warning, unreadable
(is-a-directory) and unpersistable paths -> valid token, never raises,
behavioral atomicity proof (the final path is never opened for writing —
only the .tmp path), save/load round-trip, corrupt tokens.json -> {}
preserved, source contract (no bare writes remain). Wired as preflight t117.
