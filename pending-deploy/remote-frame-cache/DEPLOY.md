# remote-frame-cache — deploy notes

## What
`manager/app.py`: new `get_remote_screenshot(wing, prov_id, name)` — a
100ms-TTL cache keyed by `(prov_id, name)` for remote (wing) instance
screenshots, now used by all three streaming paths (MJPEG `_handle_ws`,
H.264 broadcaster `frame_source`, WebRTC `_get_webrtc_frame`). Before,
each MJPEG viewer thread and each WebRTC track did its own HTTP screenshot
fetch per frame: N viewers on a remote instance = N x 20 fetches/sec
against the wing node. Now at most ~10 fetches/sec per instance no matter
how many viewers are attached.

On fetch failure the entry is dropped and None returned — never serve
stale, so the 15s frameless guards on all three paths still see a dead
source. Best-effort, never raises. No protocol, manager API, or dashboard
change.

## Deploy
Rides apply-all from HEAD (`manager/app.py` already in the stage list).
Standalone:

```
scp ~/workspace/droidfarm/manager/app.py root@<host>:/root/jerusalem/app.py
ssh root@<host> systemctl restart jerusalem
```

## Verify
- `systemctl is-active jerusalem` -> active
- Dashboard loads (HTTP 200); open a remote instance viewer — stream works.
- Two viewers on the same remote instance: wing-node side sees ~10
  screenshot serves/sec total, not ~40.
- Stop the instance mid-view: viewer shows "congelada" then the 15s guard
  trips (no frozen-forever frame from a stale cache entry).
