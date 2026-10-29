# keepalive-v2 — Roblox keepalive crash-loop circuit breaker

## Problem
The in-container `roblox-keepalive.sh` tapped the launch sequence
(HOME → drawer → tap icon) every 10s forever whenever Roblox lost focus.
If the client crashes on launch (bad build, OOM, moved launcher icon),
that is ~360 doomed launch attempts/hour/instance with zero visibility —
burning CPU on the 8-vCPU box and masking the real problem.

## What changed
- `waydroid-image/roblox-keepalive.sh` (NEW, canonical): launch attempts are
  counted in a 1-hour sliding window (state in the shared data volume, so it
  survives container restarts). Over budget (default 12/hour) it writes
  `keepalive.degraded` — reason on the first line, the same convention the
  host watchdog uses — and backs off to one attempt every 5 min. When Roblox
  is focused again, marker + state are cleared. No taps while the package
  isn't installed yet (first-boot install). Launch taps unchanged
  (monkey/am don't work; same 1024x608 coords as the dashboard button).
- `waydroid-image/entrypoint.sh`: copies the canonical script from
  `/opt/roblox-keepalive.sh` (new `Dockerfile` COPY) into the data dir at
  boot; falls back to the legacy inline version with a WARN if the image
  predates the file.
- `waydroid-image/df-watchdog.sh` (v8): new check 3b mirrors
  `keepalive.degraded` into the instance `.degraded` marker → the dashboard
  "needs attention" badge shows the reason (existing end-to-end plumbing,
  no manager/dashboard changes needed). Not routed through `cb_guard`
  (surfacing, not auto-recovery); a pre-existing circuit-breaker marker is
  never clobbered; auto-clear waits for the keepalive to recover.

## Tests
`test_keepalive_v2.sh` — 38/38 pass (hermetic, fake dumpsys/input/pm/docker):
keepalive behavior (no taps when focused, budget→backoff→marker, recovery
clears, no taps during install, corrupt/expired state handling, dumpsys
failure semantics, marker refresh) + watchdog mirroring (marker created with
reason, no log spam on rerun, auto-clear + recovered log, healthy regression,
empty-marker default reason, cb marker not clobbered).

## Deploy
1. Rides `pending-deploy/apply-all` from HEAD (no SSH-session action beyond
   the normal queue run):
   - `roblox-keepalive.sh` staged → installed to `/root/jerusalem/roblox-keepalive.sh`
     (canonical on-VPS source for the push below).
   - `df-watchdog.sh` (v8) → `/usr/local/bin/df-watchdog.sh` (minute cron
     picks it up, no restart needed).
2. Push to LIVE containers (the entrypoint change only affects new
   containers / rebuilt images): on the VPS as root,
   `bash /root/jerusalem-deploy/push-keepalive.sh` — copies the staged file
   into each booted `wd-*` data volume and restarts its keepalive loop.
   Verify: script prints "keepalive v2 running" per instance.
3. New instances get v2 automatically after the next
   `waydroid-instance:latest` image rebuild (Dockerfile COPYs the file to
   `/opt/roblox-keepalive.sh`; entrypoint installs it at boot).
4. Verify end-to-end: `cat /var/lib/docker/volumes/wd-rbx1-data/_data/data/media/0/jerusalem/roblox-keepalive.sh`
   contains "circuit breaker"; dashboard shows no spurious "needs attention"
   badges; `/var/log/df-watchdog.log` clean.

## Out of scope
- Remote (wing) nodes: their keepalive comes from the node image/entrypoint,
  not the agent. The agent 1.0.3 selfwatch re-execs the script from the data
  dir, so a future agent could fetch the canonical file from the manager
  (now staged at `/root/jerusalem/roblox-keepalive.sh`) — queued as future
  work; vultr still needs Penta's installer re-run regardless.
