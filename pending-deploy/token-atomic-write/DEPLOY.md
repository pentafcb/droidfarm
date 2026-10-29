# token-atomic-write — DEPLOY

## What
`get_static_token()` and `save_tokens()` in `manager/app.py` wrote their
state files with a bare `open(path, "w")` + `write` — the only two state
writers in the manager that were NOT tmp+rename atomic. A crash (kill -9,
ENOSPC, power loss) between the truncate and the write left a truncated
file behind:
- truncated `static_join_token` -> next boot read the short token, failed
  the len>=32 check, silently REGENERATED the "static" join token with no
  log line -> every saved node-enrollment one-liner started 403ing with no
  visible cause (same mystery-403 class as the 2026-10-06 Windows one-liner
  incident).
- truncated `tokens.json` -> next boot's `load_tokens()` hit JSONDecodeError
  and logged EVERY dashboard session out.

Fix: both write tmp+rename (same idiom as save_providers/save_instances);
`get_static_token()` prints a loud WARNING when it regenerates over an
existing-but-invalid/unreadable file, and degrades to an in-memory token
(never crashes the manager) when the file cannot be persisted.

## Deploy
Rides apply-all from HEAD — `manager/app.py` is already in the stage list.
No new stage files, no restart beyond the normal apply-all restart.

## Verify (post-deploy)
1. `systemctl is-active jerusalem` -> active (manager boots with the change).
2. Dashboard login works; close + reopen the browser -> still logged in
   (tokens.json written atomically, no logout).
3. Nodes modal "Add node" one-liner still carries the same static token as
   before the deploy (no silent rotation): compare the token in the modal
   against the pre-deploy value if recorded.
4. Optional adversarial check on the VPS (root): `head -c 5 /dev/urandom >
   /root/jerusalem/static_join_token`, restart jerusalem, confirm the
   journal shows the `[join-token] WARNING ... regenerating` line and the
   dashboard issues a fresh token (do NOT do this on a box with enrolled
   nodes unless you intend to re-issue their one-liners).
