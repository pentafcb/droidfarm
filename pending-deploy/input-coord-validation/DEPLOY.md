# DEPLOY: input-coord-validation

## What

Viewer tap/swipe coordinates are normalized 0..1000 by the dashboard,
but neither server validated them. Python's `json.loads` accepts
`Infinity` and `NaN`, so a crafted tap with x=Infinity reached
`int(float(x))`: `int(inf)` raises `OverflowError`, which neither
except clause caught (both caught only `TypeError`/`ValueError`) —
the request thread died and the connection dropped with NO response
instead of a clean 400. Out-of-range coords (e.g. 1500) were likewise
passed straight through to `input tap`.

Now (same contract both sides): coords are float-converted, then
non-finite and out-of-range values are rejected (NaN fails every
comparison, inf fails the upper bound). The manager's `scale()` returns
`None` -> clean "x/y required" 400 (tap and swipe share `scale()`); the
wing agent gains `_agent_tap_cmd(data)` (same shape as the existing
`_agent_text_cmd`) and the do_POST tap branch calls it, so a bad tap
is a clean 400 "invalid input". Valid inputs are byte-identical to
before (0, 500, 1000 boundaries, string numerics, truncation).

Files changed: `manager/app.py` (`scale()` in `do_instance_input`),
`wing/agent.py` (1.0.42 -> 1.0.43, new `_agent_tap_cmd`, tap branch
rewired). No dashboard change (it already clamps 0..1000); no wire
protocol change. All VERSION-tuple suites widened, exact pins moved
per the bump convention; remote-autologin snapshots refreshed to HEAD.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py` and `wing/agent.py`; no new stage files. The wing
agent self-updates to 1.0.43 on the next heartbeat after the manager
deploy; the vultr node additionally needs Penta's installer re-run
only if its agent predates self-update (same re-run already needed
for earlier agent versions).

## Standalone (if apply-all is unavailable)

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp wing/agent.py root@209.222.10.93:/root/jerusalem/agent.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify

1. `systemctl is-active jerusalem` -> active.
2. Dashboard root returns HTTP 200.
3. A normal viewer tap still lands (tap 500,500 -> `input tap 512 304`
   in the container's input path).
4. `POST /api/instances/<n>/input {"type":"tap","x":1e999,"y":500}`
   (JSON `1e999` parses to inf) returns 400 with an error body —
   not a dropped connection — and no input is delivered.
5. `pending-deploy/input-coord-validation/test_input_coord_validation.py`
   passes 38/38.
