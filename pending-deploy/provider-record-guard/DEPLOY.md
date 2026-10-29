# DEPLOY: provider-record-guard

## What

One layer sideways from t152/t153: those fixed the providers FILE
shape (non-dict coerces to {}) and unreadable files, but a RECORD
inside a valid dict file can still be the wrong shape — a hand edit or
partial write can leave a string record, a string last_seen, or string
stats. Every caller that trusted the record shape then crashed the
handler thread with no response:

1. `create_remote_instance` / `resize_remote_instance`: a truthy
   string record passed `if not p`, then `p.get("last_seen")` raised
   AttributeError; a string last_seen raised TypeError on the recency
   arithmetic. Both are request paths (create, resize).
2. `pick_provider` (provider="auto" create path): a non-dict record
   raised AttributeError on `p.get("stats")`; string stats fields
   raised TypeError on the capacity arithmetic.
3. `GET /api/providers` list: one non-dict record raised AttributeError
   on `p.get("name")` and killed the WHOLE list; a string last_seen
   raised TypeError on the online computation; a string host_caps
   raised AttributeError on `.get("loop_devices")`.
4. `wing_request`: a record missing url/agent_key raised KeyError and
   killed the caller's handler (every remote path funnels through it).
5. Register route: a pre-existing corrupt record for the same id
   raised AttributeError on `providers.get(pid, {}).get("created")`
   and dropped the PUBLIC register request.

Now (manager/app.py only, no agent change, no dashboard change, no wire
protocol change): `provider_last_seen()` / `provider_stats()` helpers
(non-dict or non-numeric -> 0 / {}, so "recency unknown" always reads
as offline, never online); create/resize treat a non-dict or empty
record as "node not found" and an unprovable recency as "node
offline"; `pick_provider` skips unprovable records as candidates; the
providers list skips non-dict records and degrades bad fields
(last_seen None, stats {}, selfwatch {}, caps None); `wing_request`
fails closed with 502 on a bad record; register preserves `created`
only from a dict record. Valid records behave identically.
The wing agent is untouched (still 1.0.45), so no VERSION bump and no
agent self-update rides this package.
Remote-autologin app.py snapshot refreshed to HEAD.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py`, so no new stage files. The preflight runs
`pending-deploy/provider-record-guard/test_provider_record_guard.py`
as t154 (exact-count `PASS=60 FAIL=0`).

Standalone fallback (SSH-capable session only): copy
`manager/app.py` to `/root/jerusalem/app.py` on the VPS, then
`systemctl restart jerusalem` and verify with `systemctl is-active
jerusalem` plus a dashboard load. After deploy, a corrupt provider
record degrades to "node not found" / "node offline" / skipped-list-row
instead of dropping requests, and a record missing url/agent_key
reads as "node unreachable" (502) instead of crashing the caller.

## Verify

- `python3 pending-deploy/provider-record-guard/test_provider_record_guard.py`
  prints `PASS=60 FAIL=0` (regression proof, helpers, real
  create/resize with stubbed providers/instances/wing, real
  pick_provider, real wing_request guard, providers-list field
  degradation, source contracts + snapshot parity).
- Full `--stage-only` dry run green (queue-audit 0 findings, all
  suites green, py_compile + bash -n + JS gate clean).
