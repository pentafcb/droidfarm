# DEPLOY: mjpeg-terminal-guard

## What
MJPEG fallback stops retrying after 3 consecutive closes of sockets that
never upgraded (see README.md). A socket that upgraded and then closed
keeps the existing 3s retry (frameless-guard / network-blip recovery).

## How
Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/static/index.html`; no new stage files, no manager change, no
restart needed (the static file is served from disk per request).

## Standalone (if apply-all is unavailable)

```sh
scp manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html
```

No `systemctl restart` needed for this change alone (served from disk).

## Verify
1. Dashboard root returns HTTP 200; served `index.html` contains
   `mjpegRetryAfterClose`.
2. Sign in, open a viewer on a running instance: the stream still
   connects (WebRTC -> H.264 -> MJPEG chain untouched) and a transient
   drop still recovers (opened-then-closed sockets keep retrying).
3. Break the MJPEG handshake while the viewer is open (e.g. stop the
   instance, or expire the session token): after 3 failed handshakes
   (~9s) the viewer stops reconnecting and `rbxState` reads
   "No se pudo conectar a la transmisión — revisa la instancia o
   vuelve a abrir el visor" instead of hammering the manager forever.
4. Close and reopen the viewer: the guard resets and the stream
   connects again normally.
