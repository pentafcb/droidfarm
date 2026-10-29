# node-daemon-guard — wing agent 1.0.39

The follow-up the watchdog-daemon-guard session flagged as next: the
wing agent's selfwatch had the same docker-down flaw as the main-node
watchdog. A dead dockerd looked exactly like dead containers, so the
per-instance container-down branch fired for every remote instance,
burned the 6/hour circuit budget within the hour, and marked the whole
node DEGRADED while the real cause was never repaired.

## Changes (`wing/agent.py` only)
- `VERSION` 1.0.38 → 1.0.39 (boot-recovery precedent: significant,
  operationally visible behavior change; all VERSION-tuple suites
  widened, 5 exact-version assertions moved per the bump convention).
- New `sw_check_docker_daemon(st_dir)`: `docker info` probe, 15s
  timeout (a 124 hang counts as down, not unknown). Healthy → strikes
  reset, `docker.degraded` marker + restart event file cleared with a
  "recovered" log line. Down → strike file bumped, one log line per
  pass; at 2 strikes, `_sw_cb_guard` (2/hour) gates a
  `systemctl restart docker`; strikes reset after the attempt whether
  it succeeds or fails. Beyond budget → `docker.degraded` marker
  (reason on first line), alert-only, no restart spam, no repeat
  DEGRADED log lines. Never raises.
- `selfwatch_pass_once()` (held branch): the daemon guard runs FIRST.
  While down, boot recovery + the per-instance loop are skipped;
  docker-independent guards (log trimmer, loop pool, memory watch)
  still run, and `sw_check_disk(docker_down=True)` still fires the
  disk alert WARN but skips the prune attempt (no budget burn).
- `sw_check_disk(..., docker_down=False)`: new optional flag; when
  true and pressure is >= 90% with no marker, logs the skip WARN and
  returns before noting any prune event. Relief path unchanged.

## Surfacing (no manager/dashboard change)
`selfwatch_state()` already maps every `*.degraded` file, so
`docker.degraded` flows through the existing heartbeat →
`/api/providers` → dashboard Nodes "needs attention" badge path.

## Tests
`test_node_daemon_guard.py` — 55/55 hermetic (fake docker/run, temp
state dir): version + ordering, healthy no-op, strike 1, strike-2
restart (ok + failed), 2/hour budget trip + no spam, recovery
clears marker + event file, hung/raising probe counts as down,
garbage strike file, full `pass_once` integration both branches,
disk gate while down, degraded-map surfacing, source contracts.

## Deploy
Rides apply-all from HEAD (t131). See DEPLOY.md. The node self-updates
to 1.0.39 on the next heartbeat after the manager deploy.
