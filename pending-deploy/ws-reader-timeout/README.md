# ws-reader-timeout

The 10s socket timeout on the MJPEG/H.264 stream sockets was being read
as a client disconnect, so every viewer on those transports died ~10s
after opening and reconnected forever.

## The bug

Both stream handlers set the same timeout for the same reason:

- `_handle_ws` (MJPEG) and `_handle_ws_h264` both do
  `sock.settimeout(10)` and run a reader thread that blocks in
  `ws_read_frame(sock)`, treating `(None, None)` or opcode `0x8` as
  "client closed" and setting `stop`.
- The timeout exists so the reader wakes periodically and can notice
  `stop` (a blocked `recv` is not reliably interrupted by another
  thread closing the socket) — it was never meant to mean "closed".
- But `ws_read_frame` caught `(OSError, struct.error)` and returned
  `(None, None)`, and `socket.timeout` IS an `OSError` subclass, so an
  idle recv timeout was indistinguishable from a real close.
- The dashboard client never sends frames on these sockets: the MJPEG
  and H.264 sockets are recv-only (taps/keys go over the WebRTC data
  channel, or the HTTP input fallback). So the timeout was GUARANTEED
  to fire ~10s after every viewer opened, on both transports.
- The damage was masked by the reconnect machinery: MJPEG retries an
  opened-then-closed socket after 3s (by design, for real blips), and
  H.264 failure falls back to MJPEG — so the viewer showed a
  perpetual ~10s-up / ~3s-reconnect cycle instead of a clean failure,
  and the stall badge flickered with it. WebRTC viewers were never
  affected (media does not travel over these sockets).

## What changed

`manager/app.py` only:

- New `WS_READ_TIMEOUT = -1` sentinel, with a comment explaining the
  wake-up semantics.
- `ws_read_frame()` catches `socket.timeout` BEFORE `OSError` and
  returns `(WS_READ_TIMEOUT, None)`. Order matters precisely because
  of the subclass relationship.
- Both readers `continue` on the sentinel (keep waiting) and still
  stop on `None` (close/error) or `0x8`. The 10s wake-up is preserved:
  an externally-set `stop` is still noticed within one timeout
  interval, and the 15s frameless guard still owns dead sources.

Deliberate boundaries: no dashboard change (the client already
behaves correctly), no wing agent change (the agent has no websocket
stream sockets), no VERSION bump (no protocol change — the sentinel
is internal to the manager process).

Caveat, documented in the code: a timeout mid-frame drops the partial
frame. Only a client sending partial frames and then stalling >10s
hits it; browsers send complete frames atomically, and this client
sends nothing at all.

## Tests

28/28 hermetic checks in `test_ws_reader_timeout.py` (t145):

- Sentinel value/distinctness + the `socket.timeout` subclass fact.
- Source contracts: timeout handler precedes the OSError handler,
  exactly two reader continue-sites, exactly two call sites, both
  handlers keep `settimeout(10)`, dashboard sends nothing on either
  socket.
- Real socketpair behaviour of `ws_read_frame`: three consecutive
  idle timeouts all return the sentinel; masked text/binary frames
  (including the extended-length form) round-trip; close frame and
  peer close still yield `0x8` / `(None, None)`.
- Reader-loop mirrors of the patched readers: the loop survives
  several idle timeouts, ignores a data frame, stops on a real close
  frame and on peer close, and notices an externally-set stop
  promptly via the timeout wake-up.

Regression proof for the old semantics: an idle `recv` on a
timeout-configured socket raises `OSError` (verified directly), so
the old handler necessarily returned `(None, None)` — the exact
value the readers treated as close.

Neighbour regressions green: stream-frameless-guard 24/24,
mjpeg-dedup 30/30, remote-fetch-exception-cache 27/27,
mjpeg-terminal-guard 35/35.
