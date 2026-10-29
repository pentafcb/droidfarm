# h264-init-pin

The H.264 subscriber's skip-ahead eviction could drop the one chunk
the viewer cannot decode without — the fMP4 init segment — and the
dashboard treated that as a fatal H.264 failure, downgrading the
viewer to MJPEG for the rest of the session.

## The bug

`H264Subscriber` (manager/h264_stream.py) holds a bounded queue of
fMP4 bytes for one viewer: the init segment first, then moof-aligned
fragments. When a slow viewer's queue exceeded `max_queue_bytes`
(1MB), `_push()` dropped the oldest chunk unconditionally:

```python
while self._queued > self._max and len(self._q) > 1:
    old = self._q.popleft()
```

For a fresh subscriber the oldest chunk is the init segment
(ftyp+moov). It is queued by `subscribe()` and normally drained by
the handler loop's first `read()` immediately — but if the handler
thread is delayed (load, scheduling) while the broadcaster keeps
pushing 20fps fragments, >1MB can queue before that first read, and
the init is the first thing evicted. The viewer then receives bare
moof fragments with no init segment: the dashboard's `pump()` calls
`appendBuffer()` on a SourceBuffer that was never initialized, it
throws, and `h264Fail()` closes the socket and falls back to MJPEG —
permanently for that viewer session (no H.264 retry until the viewer
is reopened). The stream itself was healthy; only the viewer
downgraded, silently.

Mid-stream skips do NOT need this treatment: dropping oldest
fragments mid-stream starts the viewer at a P-fragment at worst, and
the next IDR (GOP 20 at 20fps, plus the 2s forced-keyframe floor)
re-syncs the decoder within ~1-2s with no downgrade. Only the init
segment is irreplaceable — the broadcaster never re-sends it to an
existing subscriber.

## What changed

`manager/h264_stream.py` only (`H264Subscriber`):

- `__init__` sets `_init_pinned = True` when an init segment is
  queued (False for the empty-init case).
- `_push()` eviction: while pinned, it drops `self._q[1]` (the
  second-oldest chunk) instead of `popleft()`; a queue of `[init]`
  alone (len == 1) evicts nothing, so the loop always terminates.
  Net effect under pressure before the first read: `[init, newest]`.
- `read()` clears `_init_pinned` when it drains the queue — the init
  has been handed to the viewer, so the pin is spent and later
  eviction is plain oldest-first again.

No manager, dashboard, or agent change; no VERSION bump (the
broadcaster's wire protocol is unchanged — init first, then
moof-aligned fragments, exactly as before).

## Tests

`test_h264_init_pin.py` (hermetic, no ffmpeg — `H264Subscriber` is
pure Python): init pinned under pressure (init + newest fragment
survive, accounting exact), an inline re-implementation of the old
loop proving it drops the init, pin release after the first drain,
no-init normal eviction, a single huge fragment unable to evict the
init, under-max ordering, closed-subscriber behavior, empty-queue
read timeout, and source contracts for the eviction/pin code.
