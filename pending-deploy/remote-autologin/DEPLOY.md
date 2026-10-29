# DEPLOY: remote-node Roblox auto-login (+ WebRTC signaling fix)

Built and unit-tested 2026-10-06 ~17:40 UTC. Deploy blocked on VPS SSH access
(no password available in the worker session; Penta asleep). Next session with
SSH: run the commands below.

## What changed
- `wing/agent.py` -> 1.0.2: `/api/agent/instances/{name}/input` now accepts
  `{"type":"text","text":"..."}` / `{"type":"text","text":"<b64>","b64":true}`
  (sanitized server-side, eval-safe); `instance_status_linux` adds `started_at`
  (container StartedAt epoch) so the manager can detect first boot on remote nodes.
- `manager/app.py`: `roblox_login()` now routes by provider. Remote path checks
  agent version >= 1.0.2, launches Roblox via the agent, then sends the tap/text
  login sequence (verified 1024x608 coords, password sent base64-only, never raw).
  `auto_login_watcher` covers remote instances with the same bounded rules
  (first-boot 30-min window, 3 attempts, login_boot marker). The dashboard
  `POST /api/instances/{name}/roblox-login` no longer rejects remote nodes.
- Old vultr agent (1.0.1) fails gracefully: "node agent too old for auto-login
  (needs 1.0.2+) - re-run the node installer on that machine".

## Deploy commands (from ~/workspace/droidfarm)
```bash
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp wing/agent.py root@209.222.10.93:/root/jerusalem/wing/agent.py
scp manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html   # WebRTC ICE-gathering fix (also pending)
ssh root@209.222.10.93 "systemctl restart jerusalem && sleep 3 && systemctl is-active jerusalem"
```

## Verify
1. `systemctl is-active jerusalem` -> active.
2. Dashboard loads; Accounts panel + Login button work for rbx1-5 (local path unchanged).
3. `curl` the manager's own endpoints (needs dashboard token): instance list still shows rbx1-5.
4. After Penta re-runs the node installer on the vultr box (gets agent 1.0.2),
   check `/api/agent/health` on that node reports version 1.0.2, then test
   `POST /api/instances/srt1/roblox-login` (needs saved credentials).
5. WebRTC: open a viewer, expect "Live (WebRTC)"; if it still falls back to MJPEG
   after the signaling fix, check VPS inbound UDP, then consider coturn (TURN).

## Tests (run locally, all pass)
`test_remote_autologin.py` in this dir (32 checks, `python3 test_remote_autologin.py`;
rebuilt 2026-10-06 ~19:55 UTC after the /tmp original was evicted). Covers:
agent VERSION=1.0.2, `sanitize_input_text` eval-safety (spaces->%s, metachars
stripped, 200 cap, b64 round-trip), tap coords scaling back to exactly
(512,228)/(512,296)/(512,367), `_version_at_least` (incl. empty/garbage),
remote login input sequence (tap/text/tap/text/tap via mocked wing_request),
base64-only password transport (raw secret never on the wire), old-agent guard
(1.0.1 -> "too old for auto-login", zero inputs sent), unknown-node and
stopped-instance refusals. Local path untouched.
