# wing-installer-agent-check — DEPLOY.md

## What changed

`wing/install-wing.sh` (the Linux node installer): the agent.py download block
now verifies the download instead of trusting `[ -s ]` alone.

Before: a bad join token returned HTTP 403 with a JSON error body
(`{"error": "invalid join token"}`); `curl -sSL` (no `--fail`) wrote that body
to `$WDIR/agent.py`, `[ -s ]` passed it as a "download", and the installer
marched on to registration — which then burned 6 x 10s retries before failing
with a confusing message. A proxy serving an HTML error page as 200 sailed
through the same way.

After: `curl --fail` (4xx/5xx never writes a body) plus a marker check — the
downloaded file must start with the real agent.py shebang
`#!/usr/bin/env python3` (same idea as the .bat one-liner's `@echo off`
check). A bad download fails closed with
`could not download agent - check join token`.

## Deploy (rides apply-all — supersedes the old manual scp)

`install-wing.sh` and `install-wing.bat` are now in the apply-all stage
list (`wing-installer-staging`, staged to `/root/jerusalem/wing/`), so
this fix ships with the normal queue run — no manual scp, no restart
needed beyond the queue's own (the manager serves installers from disk
per request, placeholders replaced at serve time). The one-off manual
recipe is kept below for reference only (e.g. hot-patching a single
box without a full queue deploy).

```bash
# from ~/workspace/droidfarm (manual, for reference):
scp wing/install-wing.sh root@64.177.49.68:/root/jerusalem/wing/install-wing.sh
scp wing/install-wing.sh root@209.222.10.93:/root/jerusalem/wing/install-wing.sh
```

## Verify (on the VPS as root)

```bash
bash -n /root/jerusalem/wing/install-wing.sh && echo SYNTAX-OK
grep -c -- '--fail' /root/jerusalem/wing/install-wing.sh        # expect 1
grep -c "check join token" /root/jerusalem/wing/install-wing.sh # expect 1
# no apostrophes inside the outer bash -c '...' single-quoted block
# (an apostrophe would close the quote and break the installer):
awk '/^# BEGIN agent-download-check/{f=1} f; /^# END agent-download-check/{f=0}' \
  /root/jerusalem/wing/install-wing.sh | grep -c "'"; : # expect 0
```

## Test

```bash
# from ~/workspace/droidfarm:
bash pending-deploy/wing-installer-agent-check/test_wing_installer_agent_check.sh
# expect: 20 passed, 0 failed
```

Hermetic: drives the real download block (verbatim-extracted) with a fake
curl — good download, 403 with `--fail`, 403 JSON body, HTML error page,
empty file, wrong shebang — plus source contracts (`--fail` present, marker
present, fail message names the token, retry loop intact, `__WING_NAME__` sed
still works after a good download, no legacy `droidfarm` refs, `bash -n`).

## Rollback

Re-copy the previous `install-wing.sh` (kept in the VPS backup dir or
`git show HEAD~1:wing/install-wing.sh`). The change is installer-only: no
running agent or manager behavior changes.
