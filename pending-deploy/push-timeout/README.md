# push-timeout — bounded docker calls + progress in the finish-deploy push scripts

## Problem

The morning's one-command post-deploy (`finish-deploy.sh` steps 1–4) pushes
the canonical in-container loops into every live `wd-*` container via four
scripts: `push-keepalive.sh`, `push-booted-watcher.sh`, `push-screencap.sh`,
`push-optimizer.sh`. Three of them issued `docker exec` / `docker cp` /
`docker ps` with NO timeout — on a wedged docker daemon or a wedged Waydroid
session (the exact hang class `watchdog-timeout` v13, `keepalive-probe-timeout`
v3 and `watcher-probe-timeout` fixed everywhere else), the interactive run
hung forever at the first stuck instance with no progress indication and no
way to tell "still working on rbx7" from "stuck on rbx3". (`push-optimizer.sh`
already bounded its per-instance waydroid run at 60s, but its `docker ps`
listing was unbounded.) Flagged but left unbuilt in the 2026-10-21 security
audit.

## What it does

All four scripts share the same `dex()` wrapper idiom (standalone scripts,
so the small block is duplicated, like `BoundedHTTPServer` in manager+agent):

- `dex()` runs every docker call under GNU `timeout "$DEX_TIMEOUT"`.
- `DEX_TIMEOUT` defaults to 30s, is env-overridable, and garbage values
  sanitize back to 30. When the `timeout` binary is missing, `dex()` falls
  back to bare `docker` (never breaks the push over it).
- The `docker ps` listing fails closed: rc != 0 (timeout = 124 on a wedged
  daemon) prints `FAIL: docker ps failed (rc=...) - docker daemon may be
  wedged` and exits 1 — an empty instance list can no longer silently
  "succeed" while pushing nothing.
- Per-instance `[i/N]` progress lines (`[3/20] rbx3: keepalive v2 running`)
  so the operator sees exactly where a hang was. Message texts are otherwise
  byte-identical (existing suites assert on substrings).
- The backgrounded loop starts (`nohup ... &`) are bounded too — the
  follow-up `pgrep` verify decides honestly, and a missed start self-heals
  via the minute-cron watchdog (already the documented fallback).
- `push-optimizer.sh` keeps its existing 60s `OPT_TIMEOUT` on the waydroid
  run; only its listing moved to `dex()`.

## Files

- `pending-deploy/keepalive-v2/push-keepalive.sh` — `dex()` + bounded
  listing + progress.
- `pending-deploy/booted-watcher/push-booted-watcher.sh` — same (covers
  `docker cp` too).
- `pending-deploy/adaptive-screencap/push-screencap.sh` — same.
- `pending-deploy/android-optimizer/push-optimizer.sh` — bounded listing +
  progress (waydroid run already bounded).
- `pending-deploy/push-timeout/test_push_timeout.sh` — 66/66 hermetic:
  fake docker with hang modes, real `timeout` integration, no-timeout
  fallback, garbage-timeout sanitize, fail-closed listing, `[i/N]`
  progress, and source contracts (no unbounded `docker exec`/`cp`/`ps`
  call sites remain).

## Test notes

Two test-side issues fixed during development (no product changes): the
fake docker couldn't observe the `timeout` prefix (it's a separate process)
— a fake `timeout` that logs then execs the real binary was added; and the
source-contract grep matched the quoted `"docker cp FAILED"` message string
— contract greps now strip double-quoted strings first.
