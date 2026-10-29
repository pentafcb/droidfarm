# login-ratelimit-tunnel

Proxy-aware client keys for the manager's public rate limiters.

## Problem

`/api/login` (10 failed logins / 60s / IP → 429) and
`/api/providers/register` (10 registrations / 60s / IP → 429) keyed on the
TCP peer. Behind the Cloudflare tunnel every dashboard user shares the peer
127.0.0.1, so one user's mistyped passwords locked out everyone on the tunnel.

## Fix

`_client_ip_from_headers(headers, peer)` in `manager/app.py`:

- peer is loopback → key = `CF-Connecting-IP`, else leftmost `X-Forwarded-For`
  entry; must parse as an IP literal (stdlib `ipaddress`), else the peer.
- peer is anything else → the peer, headers ignored (spoof-proof).
- Never raises; worst case degrades to the old peer-keyed behavior.

Both routes derive the key through the helper before any limiter call.

## Preflight

t85: `python3 pending-deploy/login-ratelimit-tunnel/test_login_ratelimit_tunnel.py`
(exact-count `"34 passed, 0 failed"`).
