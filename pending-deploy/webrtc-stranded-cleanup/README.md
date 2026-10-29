# webrtc-stranded-cleanup

## The gap
Two independently correct packages left a hole between them:

- **t42 webrtc-stream-want**: a WebRTC viewer opens an adaptive-screencap
  viewer mark (fast 0.12s in-container capture). Cleanup of that mark
  happens in `_close_pc`, reached via the pc's stale-handshake sweep
  (`call_later`), its `connectionstatechange` handler, or the frameless
  guard — all scheduled on, or executed by, the webrtc event loop.
- **t59 webrtc-loop-supervisor**: when the webrtc-loop thread dies, the
  supervisor builds a fresh loop via `_webrtc_loop_thread_factory()`.

Neither handled the stranded peer connections: every cleanup path for a
live pc was bound to the dead loop, and the factory never touched the
registry. Each stranded pc's viewer mark pinned its instance's capture
loop fast forever — permanent CPU burn with no dashboard signal.

## The fix (manager/app.py only)
- `_webrtc_pcs[pc_id]` stores `(pc, provider, instance_name)`.
- New `_webrtc_force_close_all()`: pops every registry entry, calls
  `stream_viewer_close(provider, instance_name)` per entry, tolerates
  legacy bare-pc values and garbage, never raises. Runs on the
  supervisor thread from the factory (the registry is quiescent then:
  all writers execute on the dead loop thread). One WARN to the
  watchdog log → visible in the dashboard Eventos modal.
- `webrtc_offer_sync`: `run_coroutine_threadsafe` moved inside the
  fail-fast try (submit race vs a dying loop → clean None → the
  dashboard falls back to H.264/MJPEG).

Deliberately NOT done: awaiting `pc.close()` on the stranded pcs — the
transports are bound to the dead loop and a cross-loop close is unsafe;
they are left for GC. The viewer mark (the real resource: fast-capture
CPU) is what gets reclaimed.

## Tests
`test_webrtc_stranded_cleanup.py` — 26/26 hermetic (real factory, real
loop, real `_webrtc_force_close_all`, redirected watchdog log +
stream-want base; fake dead old loop; raising-submit race).
