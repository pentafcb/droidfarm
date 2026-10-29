# login-ratelimit-tunnel — proxy-aware rate-limit client keys

## What it is

The `/api/login` (2026-10-08) and `/api/providers/register` (2026-10-05) rate
limiters keyed on the TCP peer IP (`self.client_address[0]`). That is correct
for direct connections — but the dashboard is also reached through the
Cloudflare tunnel, where cloudflared terminates the public connection and opens
a fresh LOCAL connection to the manager. Every tunneled dashboard user shares
the same peer (127.0.0.1), so one user's 10 mistyped passwords returned
HTTP 429 ("too many attempts") for EVERYONE on the tunnel.

This makes the limiter key proxy-aware: when the TCP peer is a loopback
address, the key is taken from the proxy-reported client IP —
`CF-Connecting-IP` first, then the leftmost `X-Forwarded-For` entry. The
candidate must parse as an IP literal or the peer is kept (fail closed to the
old behavior). Headers are NEVER trusted for non-loopback peers — they are
trivially spoofable on the open internet, and an attacker must not be able to
pick their own bucket (or evade the limit).

## Files changed (all ride apply-all from HEAD — no new stage files)

- `manager/app.py`: `import ipaddress`; new `_client_ip_from_headers(headers,
  peer)` (never raises); `/api/login` and `/api/providers/register` derive
  the limiter key through it instead of the bare peer. No dashboard change
  (the 429 body and the doLogin() 429 branch are unchanged).

## Tests

`test_login_ratelimit_tunnel.py` — 34/34 hermetic checks: loopback + both
headers (CF-Connecting-IP wins, XFF leftmost, trimming), garbage/empty
headers fall back to the peer, non-loopback peers ignore headers (spoof
proof), ::1 + 127/8 loopback handling, IPv6 normalization, never-raises on
hostile input, per-user bucket separation behind one tunnel, direct-IP
control, garbage-header degradation to the old behavior, and route contracts
for both branches.

## Deploy

No standalone deploy — rides `pending-deploy/apply-all/deploy-all.sh` from HEAD
(`app.py` is already in the stage list). Verify after deploy: 10 rapid
wrong-password POSTs from one tunneled client → 429 for that client only; a
second client (different CF-Connecting-IP) still gets 401s, not 429.

## Rollback

Revert is the previous `app.py` via the apply-all automatic rollback (or the
`.deploy-backup` copies on the VPS).
