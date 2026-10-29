# Deploy: viewer stream-freshness indicator

The dashboard viewer now shows "Imagen congelada — sin cuadros desde hace
Xs" whenever no video frame arrived for more than 3s (all transports:
WebRTC, H.264, MJPEG). A stalled stream used to look identical to a live
one; the stall is now visible, and the badge clears the moment frames
resume.

## What rides apply-all

No new stage files: `manager/static/index.html` is already in the
apply-all stage list, and this rides it from repo HEAD. Suite `t29` in
preflight: `pending-deploy/stream-freshness/test_stream_freshness.py`
(27/27).

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH (static file — no restart needed):
scp ~/workspace/droidfarm/manager/static/index.html \
    root@209.222.10.93:/root/jerusalem/static/index.html
```

## Verify

- Dashboard loads (HTTP 200); the viewer shows no "Imagen congelada"
  line while a stream is running.
- Stall: stop one instance's container (or kill its screencap loop from
  an SSH session) with the viewer open — within ~4s the amber
  "Imagen congelada — sin cuadros desde hace Xs" line appears under the
  status line; restart the loop and it clears on the first new frame.
- Close the viewer — no JS errors, interval stops (badge never appears
  on the next open until a real stall).
