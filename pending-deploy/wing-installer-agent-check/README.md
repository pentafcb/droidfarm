# wing-installer-agent-check

Hardens the Linux node installer's agent download against bad-token and
proxy error pages.

## The gap

`wing/install-wing.sh` downloaded `agent.py` with `curl -sSL` (no `--fail`)
and accepted anything non-empty (`[ -s ]`). A bad join token gets HTTP 403
with a JSON error body — which landed in `$WDIR/agent.py` and passed the
size check. The installer then chmod'd a JSON file and burned a minute of
registration retries before failing confusingly. Same for HTML error pages
served as 200 by proxies.

## The fix

`curl --fail` (error bodies never written) + a shebang marker check: the
download must start with `#!/usr/bin/env python3` or the installer fails
closed with `could not download agent - check join token`. Mirrors the .bat
one-liner's `@echo off` marker check.

## Constraints discovered while building

- The whole installer body after line 15 runs inside an outer
  `bash -c '...'` single-quoted string, so the block's comments must contain
  no apostrophes (a `'` would close the quote and break parsing — caught by
  `bash -n`, which is wired in the apply-all preflight syntax gate; the
  wing-loop-devices suite also asserts no apostrophes in the block).

## Deploy

Manual `scp` of `wing/install-wing.sh` to `/root/jerusalem/wing/` on each box
(see DEPLOY.md) — not in the apply-all stage list, no restart needed
(served from disk per request).

## Tests

`test_wing_installer_agent_check.sh` — 20/20 hermetic. Wired as preflight
t89 in `pending-deploy/apply-all/deploy-all.sh`.
