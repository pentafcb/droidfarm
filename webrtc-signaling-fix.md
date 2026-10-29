# WebRTC fix: the offer never carried ICE candidates

## Root cause (verified in code, not a guess)

`manager/static/index.html`, `startWebRTC()` does:

```js
const offer = await pc.createOffer();
await pc.setLocalDescription(offer);
const resp = await api(`/api/instances/${name}/webrtc/offer`, {
  method:'POST', body:JSON.stringify({sdp:offer.sdp, type:offer.type})
});
```

`createOffer()` returns SDP with **zero ICE candidates** — candidates are only
gathered after `setLocalDescription()`, asynchronously. The code POSTs the stale
pre-gathering `offer.sdp` instead of waiting for gathering and sending
`pc.localDescription.sdp`. The server (`_webrtc_create_offer` in `manager/app.py`)
therefore receives an offer with no client candidates at all, so no ICE candidate
pair can ever form and `connectionState` can never leave `new`/`disconnected`.
The 8s timeout then fires and the viewer falls back to MJPEG — **on any network**,
tunnel or not.

The earlier "WebRTC can't work through the Cloudflare tunnel" diagnosis was wrong:
only *signaling* goes through the tunnel (plain HTTPS POST, works fine); media
goes direct UDP browser <-> VPS. The signaling bug guaranteed failure before the
network ever mattered.

Server side is fine: aiortc 1.15.0's `setLocalDescription()` awaits its internal
`__gather()` (verified in the aiortc 1.15.0 wheel source,
`aiortc/rtcpeerconnection.py`, `setLocalDescription` ends with
`await self.__gather()`), so the answer SDP already contains the server's host
candidates. The manager runs as a systemd service directly on the VPS host (not
in Docker), so those host candidates include the VPS's public IP — provided the
VPS firewall allows inbound UDP (unverified; check at deploy).

## Fix (client only, one edit in `manager/static/index.html`)

Apply with `muse.edit` using the exact old text below.

OLD:
```js
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const resp = await api(`/api/instances/${name}/webrtc/offer`, {
      method:'POST', body:JSON.stringify({sdp:offer.sdp, type:offer.type})
    });
```

NEW:
```js
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    // Wait for ICE gathering so the offer carries our candidates (host +
    // STUN reflexive). Without this the server gets zero client candidates
    // and ICE can never connect.
    await new Promise((resolve) => {
      if (myPc !== pc || pc.iceGatheringState === 'complete') return resolve();
      const done = () => {
        pc.removeEventListener('icegatheringstatechange', done);
        resolve();
      };
      pc.addEventListener('icegatheringstatechange', done);
      setTimeout(done, 4000); // don't hang forever behind strict NATs
    });
    if (myPc !== pc) return;
    const resp = await api(`/api/instances/${name}/webrtc/offer`, {
      method:'POST', body:JSON.stringify({sdp:pc.localDescription.sdp, type:pc.localDescription.type})
    });
```

## Deploy + verify

1. Copy the edited `static/index.html` to the VPS (`/root/jerusalem/static/index.html`
   per `manager/jerusalem.service` WorkingDirectory). `_serve_static` reads the
   file from disk on every request (verified in `manager/app.py`), so no service
   restart is needed — but hard-refresh the browser (Ctrl+Shift+R) to bypass cache.
2. Check VPS firewall allows inbound UDP (aiortc binds ephemeral UDP ports):
   `ufw status` / `iptables -L -n`. If UDP in is blocked, open it or WebRTC still fails.
3. Open a viewer (tunnel URL is fine — only signaling uses it). Expect
   `Live (WebRTC)` within a few seconds. If it still falls back to MJPEG after
   this fix, the remaining suspects are: VPS firewall blocking inbound UDP, or
   the browser's network blocking UDP — then the next step is a TURN server
   (coturn) on the VPS, not more signaling changes.

## Optional follow-up (only if direct UDP still fails after the fix)

Add a STUN server to the aiortc peer connection in `manager/app.py`
(`_webrtc_create_offer`) so the server also gets a server-reflexive candidate
if the VPS sits behind NAT:

```python
from aiortc import RTCConfiguration, RTCIceServer
pc = RTCPeerConnection(RTCConfiguration(
    iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]))
```
