# wing-keepalive-mirror — agent 1.0.15 keepalive crash-loop surfacing (DEPLOY)

## What it is
Main-node df-watchdog check 3b mirrors the in-container roblox-keepalive.sh
v2's `keepalive.degraded` marker into the instance `.degraded` marker, so the
dashboard "needs attention" badge shows a crash-looping Roblox. Wing agents
never did: on a wing node the keepalive silently backed off to 5-min launch
attempts while the node badge stayed green. This is the agent-side mirror
(main-node 3b parity), surfaced through the existing
`selfwatch_state()` degraded map -> heartbeat -> dashboard Nodes badge.

## Behavior
- `sw_check_keepalive_degraded()` reads the host-side data-volume copy of
  `keepalive.degraded` (`<vol>/wd-<name>-data/_data/data/media/0/jerusalem/` —
  the same layout `sw_booted_once` reads).
- Marker present -> writes `<st_dir>/<name>.degraded` with
  `Roblox keepalive: <first line, <=160 chars>` on the first line + one
  DEGRADED log line; the instance counts as unhealthy so the step-4
  auto-clear waits for the keepalive to recover (it clears its own marker
  when Roblox is focused again).
- Surfacing only: never routed through the circuit-breaker budget, a
  pre-existing `.degraded` marker is never clobbered, nothing is restarted.
- Nodes whose image predates keepalive v2 never write the marker -> silent
  no-op. Invalid names -> "skip". All helpers best-effort, never raise.

## Deploy
Rides apply-all from HEAD (wing/agent.py already in the stage list). After
the deploy, the vultr node needs Penta's installer re-run for agent 1.0.15
(same re-run already pending for 1.0.2-1.0.14); until then srt nodes lack
the mirror and nothing misreports.

## Verify
- `python3 pending-deploy/wing-keepalive-mirror/test_wing_keepalive_mirror.py`
  -> `35 passed, 0 failed`.
- On a wing node with agent 1.0.15: crash-loop Roblox (or plant a fake
  `keepalive.degraded` in the data volume) -> dashboard Nodes modal shows
  "needs attention" for that instance with the keepalive reason; remove the
  marker -> badge clears on the next selfwatch pass.
