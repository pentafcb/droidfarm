# fleet-resources-remote — fleet bar totals cover wing nodes

## Problem

`fleet_resource_summary()` only summed the local docker-stats cache, but
the dashboard fleet bar is labelled "RAM de flota": with wing nodes in the
fleet, the RAM/CPU numbers silently undercounted usage, `instances_seen`
counted only local instances, and the denominator (`host_mem_mb`) excluded
wing-node RAM entirely. The bar looked authoritative while being wrong —
exactly the kind of number Penta would trust when deciding whether the
farm has headroom for a "Create fleet".

## Fix (manager/app.py only — no new stage files, rides apply-all from HEAD)

- `fleet_resource_summary(remote_instances=None, remote_host_ram_mb=0)`:
  remote entries (the handler's `out` rows, whose mem/cpu are already
  cleaned per-card) are summed in with re-validation; an entry counts as
  "seen" only when it reports a numeric mem, mirroring the local cache
  semantics. `host_mem_mb` becomes main-node RAM + fresh wing-node RAM.
  `cpu_pct` stays a fleet-wide SUM of per-container CPU% (docker %CPU can
  exceed 100 on a multi-core host, so the sum was never a single-host
  percentage even when local-only). Return shape is unchanged (same 4
  keys), so the dashboard renders identically — now truthfully — and the
  no-argument call behaves exactly as before.
- `remote_host_ram_mb(providers)`: sums heartbeat `stats.ram_total_mb`
  (the same field the fleet-create headroom check uses) for providers
  seen within `REMOTE_FLEET_STALE_S = 120` — the same staleness rule as
  the `/api/providers` online flag — so a dead node's RAM can't pin the
  denominator. Never raises.
- `/api/instances` handler: passes `remote_entries` (rows with
  `provider != "local"`) and `remote_host_ram_mb(providers)` into the
  summary. Dashboard `updateResCov` untouched.

## Deliberate boundaries

- A wing node that stopped heartbeating drops out of BOTH the numerator
  and the denominator; the bar's "(N reportando)" count already says the
  totals cover only reporting instances.
- No per-node breakdown in the bar: the Nodes modal already shows
  per-node info; this fix only makes the existing fleet number honest.

## Tests

`test_fleet_resources_remote.py` — 54/54 hermetic: baseline identical to
the old local-only behavior (shape keys, stale exclusion, host-read
failure → None), remote sums/counting, garbage tolerance (string/NaN/inf/
negative/over-cap/non-dict), remote-RAM denominator math incl. the
host-read-failure interaction, provider staleness (119s vs 121s) and stats
shape, handler wiring (source-level), dashboard contract, validator unit
checks. The instance-resources suite's "response carries fleet_resources"
assertion was updated to the new call form (still 51/51).
