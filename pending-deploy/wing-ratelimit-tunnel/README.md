# wing-ratelimit-tunnel

Wing agent 1.0.27 -> 1.0.28: proxy-aware agent-key rate-limit keys
(manager login-ratelimit-tunnel parity).

## The gap

The wing agent's agent-key rate limiter (1.0.19: 10 failed / 60s / IP -> 429)
keyed on the TCP peer (`self.client_address[0]`). The manager fixed the same
flaw on `/api/login` and `/api/providers/register`
(login-ratelimit-tunnel, 2026-10-20): behind a reverse proxy or tunnel every
user shares the loopback peer, so one user's 10 bad attempts 429'd everyone
— on the agent that means the manager's own keyed requests (screenshots,
status, self-update) from the same host get 429'd by a stranger's guessing.
The sixteenth "main had it, wing didn't" parity item (1.0.5 drift, 1.0.6
adaptive-screencap, 1.0.7 disk/log, 1.0.11 session-death, 1.0.12 loop
devices, 1.0.13 memory, 1.0.14 stuck-boot, 1.0.15 keepalive mirror, 1.0.16
binderfs self-heal, 1.0.17 agent HTTP self-check, 1.0.18 create image
preflight, 1.0.19 auth rate-limit, 1.0.20 status cache, 1.0.21 headroom
probe, 1.0.22 per-instance degraded, 1.0.23 selfwatch lock, 1.0.24
self-update, 1.0.25 lock race, 1.0.26 route name gate, 1.0.27 host caps).

## Wing (`wing/agent.py`)

- New `_agent_client_ip(headers, peer)` (never raises): mirrors the
  manager's `_client_ip_from_headers` exactly — loopback peer ->
  `CF-Connecting-IP` first, then the leftmost `X-Forwarded-For` entry; the
  candidate must parse as an IP literal via stdlib `ipaddress` (new import),
  else the peer is kept (fail closed to the old behavior); non-loopback
  peers never trust headers (spoof-proof).
- `Handler._check_auth()` derives the limiter key through it before any
  limiter call (health route still unauthenticated and uncounted; the
  429-before-credential ordering is unchanged). Worst case degrades to the
  old peer-keyed behavior; zero manager/dashboard changes.
- No route, protocol, or API change — the vultr node self-updates to 1.0.28
  on the next heartbeat after deploy (no installer re-run needed).

## Tests

`test_wing_ratelimit_tunnel.py` — 38/38 hermetic checks: VERSION exact
1.0.28 + manager `>=1.0.2` gate, helper units (CF/XFF priority,
leftmost/trim, garbage/empty/missing headers -> peer, non-loopback public/
private/IPv6 ignore headers, `::1` loopback, IPv6 normalization, hostname/
host:port rejected, never-raises on hostile input), `_check_auth` bucket
isolation (two tunneled users separate, manager-direct bucket untouched,
correct-key-while-limited still 429, forged header from public peer keys on
the peer, health uncounted, success clears only its own bucket), and a live
localhost server (10 wrong -> 429 with the exact body, separate bucket per
CF IP, no-header 401 not 429, health 200).
