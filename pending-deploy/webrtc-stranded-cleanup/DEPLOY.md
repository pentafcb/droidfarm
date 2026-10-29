# webrtc-stranded-cleanup — DEPLOY

## What
Fixes a real resource leak found by auditing the t59 WebRTC loop
supervisor against the t42 stale-handshake sweep: when the webrtc-loop
thread dies, every live RTCPeerConnection in `_webrtc_pcs` was stranded
— its stale sweep, state-change handler, and frameless teardown were all
scheduled on the dead loop and never fired. The t59 factory built a
fresh loop but never cleaned the registry, so each stranded pc's
adaptive-screencap viewer mark stayed pinned forever, keeping that
instance's in-container capture loop at fast 0.12s permanently (CPU
burn on the 8-vCPU box, zero dashboard visibility).

`_webrtc_pcs` values are now `(pc, provider, instance_name)` tuples and
`_webrtc_loop_thread_factory()` calls the new `_webrtc_force_close_all()`,
which pops every entry and drops its viewer mark (lock-guarded, never
raises, safe from the supervisor thread). One WARN line goes to the
watchdog log so the dashboard Eventos modal surfaces it. Also hardens
`webrtc_offer_sync`: `run_coroutine_threadsafe` moved inside the
fail-fast try (the loop could die between the `is_running()` check and
the submit — RuntimeError on a closed loop — and an offer must fail fast
to the H.264/MJPEG fallback, never escape).

## Files changed (all ride apply-all from HEAD, no new stage files)
- `manager/app.py` (already in the apply-all stage list)

## Deploy
Run from an SSH-capable session:

    bash pending-deploy/apply-all/deploy-all.sh        # old VPS target
    # or
    bash pending-deploy/newbox-sync/sync-newbox.sh --yes   # new VPS 64.177.49.68

then on the VPS: `bash /root/jerusalem/finish-deploy.sh`.

No per-package standalone deploy — app.py rides the queue. The restart
is the single `systemctl restart jerusalem` inside vps-apply.sh.

## Verify
- Manager log / Eventos modal: on the next webrtc-loop restart the WARN
  `webrtc loop restarted with N stranded peer connection(s)` appears.
- Steady state: no behavior change — registry empty on first start, the
  force-close is a silent no-op.
- Unit suite: `python3 pending-deploy/webrtc-stranded-cleanup/test_webrtc_stranded_cleanup.py`
  (26/26), wired as apply-all preflight t63.
