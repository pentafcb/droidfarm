# webrtc-handshake-cleanup

## The gap
`manager/app.py::_webrtc_create_offer` registered the new peer connection
in `_webrtc_pcs` and scheduled the 180s stale-handshake sweep *before*
running the SDP handshake. When the handshake itself failed (garbage SDP
→ `RTCSessionDescription`/`setRemoteDescription` raises, or a failed
`createAnswer`/`setLocalDescription`), nothing cleaned up: the
`RTCPeerConnection` (its threads, UDP sockets) and the registry entry sat
until the stale sweep reaped them up to 3 minutes later. The offer route
is auth-gated, so this was not an unauthenticated DoS — but a buggy
client retry loop (or a wedged loop racing `fut.result`'s 15s timeout)
could accumulate leaked pcs.

The 180s sweep was designed for *abandoned* handshakes (browser tab
killed mid-ICE); a *failed* handshake is known immediately and should be
reaped immediately.

## The fix (manager/app.py only)
The handshake (`RTCSessionDescription` parse → `setRemoteDescription` →
`createAnswer` → `setLocalDescription`) is wrapped in try/except. On
failure, the already-idempotent `_close_pc("handshake-failed")` runs at
once (registry pop, `pc.close()`), then the exception re-raises so
`webrtc_offer_sync` still returns `None` and the dashboard fails over to
H.264/MJPEG exactly as before.

Deliberately untouched:
- The viewer mark still opens only *after* `setLocalDescription`
  succeeds; `stream_viewer_close` clamps at 0, so the failure path can
  never corrupt the adaptive-screencap refcount.
- `_close_pc` stays idempotent — a concurrent `connectionstatechange`
  cleanup racing the failure is harmless.
- The 180s stale sweep still covers genuinely abandoned handshakes.

## Tests
`test_webrtc_handshake_cleanup.py` — 34/34 hermetic (fake aiortc with
per-stage failure injection, real asyncio loop in a thread, real numpy,
redirected stream-want base): wiring contract (guarded block,
`handshake-failed` reason, re-raise, mark ordering), all four failure
points (parse/remote/answer/local → exception propagates, pc closed
immediately, registry popped, no viewer mark), happy path unchanged,
post-cleanup sweep no-op, double-close idempotency. The suite fails 14
checks against the pre-fix code (regression proof).
