# wing-agent-selfupdate — wing agent 1.0.24 self-update from its own manager

## Problem

The dashboard's Nodes modal badges a node whose agent is older than the one
deployed beside the manager (`wing-agent-update`), but every version bump
still needed Penta to re-run the node installer by hand — twenty versions
(1.0.2 → 1.0.23) were pending on the vultr node at once. Nothing ever
applied the update; the badge just sat there.

## What changed

- `wing/agent.py` **1.0.23 → 1.0.24**: new self-update section.
  - The heartbeat response now carries `{"agent_update": {"version",
    "sha256"}}` when a newer `wing/agent.py` sits beside the manager
    (`heartbeat_loop` reads the response now — it was discarded before;
    a read failure can never break the heartbeat itself).
  - `maybe_agent_self_update(directive)`: strict version parse (mirrors
    the manager, so `1.0.9 < 1.0.10`), applies only when the directive is
    strictly NEWER. Gates, all fail-closed: Linux only; only under systemd
    (`sw_under_systemd`, one WARN/day otherwise — without a supervisor,
    exiting would orphan the node); at most one update per process, with a
    10-minute cooldown retry on transient fetch/apply failures (no tight
    failure loop, no silent permanent skip).
    `fetch_agent_update` pulls from the agent's OWN `MAIN_URL`
    (`/api/agent/update/download`, agent-key headers), bounded at 2MB,
    verifies the sha256 against the directive, byte-compiles the download
    (temp removed on failure). `apply_agent_update` keeps one `.bak` and
    swaps atomically via `os.replace`. Success → ALERT in `selfwatch.log`
    (surfaced by the dashboard Eventos modal, zero dashboard changes) →
    `os._exit(1)` so systemd restarts on the new code. Every helper
    best-effort, never raises.
  - A real bug was caught by the tests during development: the injected
    `_exit_fn` was called with no arguments (`list.append` needs one) —
    the contract is now `_exit_fn(1)`, mirroring `os._exit(1)`.
- `manager/app.py`:
  - `_agent_disk_path()` (single source for the deployed
    `wing/agent.py` path; `current_agent_version()` now uses it),
    `agent_file_sha256()` (cached per process, `""` when unreadable),
    `agent_update_directive(agent_ver)` → `{"version", "sha256"}` or None.
  - `/api/providers/heartbeat` response gains `agent_update` when the
    reporting agent is outdated (pre-1.0.24 agents ignore the unknown
    field — no behavior change for them).
  - New `GET /api/agent/update/download`: agent-key auth via
    `X-Provider-Id`/`X-Agent-Key` headers (hmac compare, 401 otherwise;
    key never in the query string), serves the exact on-disk agent.py
    bytes with `X-Agent-Version`/`X-Agent-Sha256` headers, refuses when
    the file is missing/changing/over 2MB. Bounded reads; never raises
    out of the route.
- Dashboard: **no change** — the update badge clears on the next
  heartbeat after the agent restarts; the Eventos modal shows the ALERT.

## Trust notes

- The agent only ever downloads from the `MAIN_URL` it was enrolled with
  (the manager it already trusts and heartbeats to every 30s); the agent
  key already travels in cleartext on that path today (plain-HTTP
  heartbeat), so the download adds no new exposure class.
- The manager is already fully trusted by the node (it can run arbitrary
  docker commands through the agent's endpoints); serving its own
  `agent.py` bytes adds no new trust.
- Rollback: `<agent>.bak` sits next to `agent.py`; `cp agent.py.bak
  agent.py && systemctl restart <service>` reverts by hand. If a pushed
  agent crash-loops, systemd restarts it on the same (new) file — no
  re-download loop is possible (the on-disk VERSION already matches).

## Deploy

Rides `apply-all` from HEAD — `wing/agent.py` + `manager/app.py` are
already in the stage list (no new stage files). After deploy, the NEXT
heartbeat (≤30s) carries the directive to any outdated node and the node
updates itself — **this is the last version that needs Penta's installer
re-run**; 1.0.25+ will flow automatically. Windows nodes skip
self-update (no supervisor) and keep the manual installer path.

## Verify

- `python3 pending-deploy/wing-agent-selfupdate/test_wing_agent_selfupdate.py`
  → `63 passed, 0 failed`
- Full `deploy-all.sh --stage-only` dry run green (preflight t81,
  queue-audit 0 findings)
- After the SSH deploy: on a node, `selfwatch.log` shows
  `ALERT agent self-updated 1.0.23 -> 1.0.24, restarting for supervisor`
  and the dashboard Nodes modal clears the update badge within a minute.

## Tests

`test_wing_agent_selfupdate.py` (hermetic, no network/docker/systemd):
VERSION exact 1.0.24 + manager `>=1.0.2` gate; `parse_agent_version` units
(incl. numeric ordering + manager-parse agreement); manager
`agent_file_sha256`/`agent_update_directive` units (older → directive,
current/newer/garbage → None); heartbeat-response contract; download-route
contract (exact path, header auth, hmac, 401, 2MB cap, version/sha headers,
raw-bytes body); `fetch_agent_update` (bad directive/version/sha/url,
sha mismatch, compile failure + temp cleanup, non-200, oversize, success
with auth headers + exact URL); `apply_agent_update` (backup + atomic swap,
fresh-install path, never-raises); `maybe_agent_self_update` (windows,
already-done, no-directive, current, older, garbage-version, no-systemd
one-WARN-per-day, fetch-fail, apply-fail + temp removed, success →
`exit(1)` once + backup + ALERT, raising fetch → error never propagates,
garbage directives never raise); `heartbeat_loop` wiring contract.
