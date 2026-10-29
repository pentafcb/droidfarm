# DEPLOY: h264-init-pin

## What

Init-segment pin in `manager/h264_stream.py`: `H264Subscriber`'s
skip-ahead eviction no longer drops the fMP4 init segment. While the
init chunk is still queued, eviction drops the second-oldest chunk
instead of the oldest, and a queue holding only the init segment
evicts nothing. The pin releases on the first drain (read() hands the
init to the viewer); eviction after that is plain oldest-first again.
Without it, a viewer whose socket stalled before its first read (the
H.264 handler thread delayed under load while the broadcaster kept
pushing 20fps fragments, >1MB queued) lost its init segment: its
first read returned bare moof fragments, the dashboard's
appendBuffer() threw, and h264Fail() downgraded that viewer to MJPEG
for the rest of the session — even though the stream itself was
healthy. See README.md.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/h264_stream.py`; no new stage files, no dashboard change,
no wing agent change, no VERSION bump (broadcaster-internal behavior;
the wire protocol is unchanged).

## Standalone (if apply-all is unavailable)

```sh
scp manager/h264_stream.py root@209.222.10.93:/root/jerusalem/h264_stream.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify

1. `systemctl is-active jerusalem` -> active.
2. Dashboard root returns HTTP 200, banner shows H.264: True.
3. Sign in, open a viewer on a running instance: the stream comes up
   as "En vivo (H.264)" (not MJPEG) and stays there.
4. Stall test (optional, needs a throttled connection): with the
   viewer open on a slow/flaky link, the stream recovers in H.264
   after the stall instead of silently sitting on MJPEG — a
   pre-fix viewer that lost its init would have downgraded and
   stayed on MJPEG until the viewer was reopened.
5. Existing H.264 behavior unchanged: `pending-deploy/h264-mse/
   test_h264_integration.py` still passes 59/59 (fan-out, late
   joiner, slow-viewer boundedness, idle self-stop).
