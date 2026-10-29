# screenshot-path-gate — deploy notes

## What it is

Manager-only hardening, same audit class as t161–t164: `capture_screenshot()`
(the shared frame source for the MJPEG, H.264, and WebRTC stream paths) now
fails closed on an invalid instance name — returns `None` before any volume
path is built or the frame cache is touched. Previously it built its docker
volume path straight from the name, so a corrupted `instances.json` key
reaching it from a future internal caller could read a file outside its own
instance directory. All stream routes already gate names; this makes the sink
itself safe. Valid names behave identically.

## Files changed (in the repo, deployed from HEAD via apply-all)

- `manager/app.py` — gate + docstring in `capture_screenshot()` only.
- No agent, dashboard, or wire-protocol change.

## Tests

`test_screenshot_path_gate.py` — 29/29 checks (hermetic, real function):
invalid-name matrix blocked with zero filesystem touches and an untouched
frame cache; valid names still read fresh new-path and old-path frames;
mtime cache hit returns cached JPEG with no second read; missing/stale
frames return `None` without reading; gate placement + sibling sink gates
kept + stream call sites unchanged + snapshot parity; negative proof that
the pre-fix path escapes the instance volume for a traversal name.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight t165).
No new stage files — `manager/app.py` is already in the apply-all stage
list. Standalone fallback (only if apply-all is unavailable):

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify after deploy

- `systemctl is-active jerusalem` → `active`.
- Dashboard root returns HTTP 200.
- A viewer on any instance still shows a live frame (MJPEG fallback path
  exercises the same `capture_screenshot()` sink).
- A corrupted instance key in `instances.json` can no longer drive a volume
  read outside its own directory (code inspection: the gate precedes the
  `cname` build in `capture_screenshot()`).
