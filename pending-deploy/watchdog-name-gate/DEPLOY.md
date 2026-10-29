# DEPLOY — df-watchdog v16 (instance-name gate on the discovery block)

## What it is
`waydroid-image/df-watchdog.sh` v16 adds a name gate to the instance
discovery block, closing a trust-boundary gap between the manager and the
watchdog.

**The gap**: the watchdog reads `instances.json` keys directly (manager
state, no validation on this side) and `check_one` builds marker paths
(`$ST/$i.degraded`, `$ST/$i.robloxver`) and container names (`wd-$i`)
straight from the key. The manager only ever creates names matching
`^[A-Za-z0-9_-]{1,32}$` (see `valid_instance_name`), but a corrupted
record — the same class the manager-side record-shape guards were built
for — or a hand-edited file could carry a key like `../../tmp/x`, and
the watchdog would then write marker files outside `$ST` and hand docker
nonsense container names. The agent side was already gated
(`check_roblox_drift` fullmatch-rejects invalid names); the watchdog side
was not.

**The fix**: the discovery block now filters `INSTANCES` to the manager's
own contract before either consumer runs (the main per-instance loop and
the `boot_recovery` call — both execute after the gate). Invalid names
are dropped, never acted on. One WARN line per cycle names the skipped
keys (first 5 + count, so a hostile file can't flood the 2MB-trimmed
watchdog log). Valid names pass through byte-identically, in order.
Nothing is deleted, stopped, or restarted by the gate — it only decides
which names the existing checks see.

## Tests
`test_watchdog_name_gate.sh` — 21/21 pass (real `log()` + the verbatim
gate block, hermetic): all-valid passthrough with no WARN, 32/33-char
boundary, traversal names dropped (`../x`, `../../tmp/evil`, `/abs`,
`a/b`), shell-metachar names dropped (`x;y`, `$(id)`, backticks, `a*b`),
mixed-list order preservation, all-invalid yields empty + WARN, exactly
one WARN line per cycle with count + first-5 truncation, WARN names the
skipped keys, empty input silent, dash/underscore/digits valid, source
contracts (gate markers present, gate after the rbx1-5 fallback and
before the v15 section, `log()` defined before the gate, both consumers
after the gate), negative proof (pre-gate input keeps the traversal
name, so the suite is not vacuous).

## Deploy (rides apply-all — no new stage files)
`df-watchdog.sh` is already in the apply-all stage list; v16 deploys from
repo HEAD. Run from an SSH-capable session:

```bash
bash ~/workspace/droidfarm/pending-deploy/apply-all/deploy-all.sh
```

Standalone (if apply-all is unavailable):

```bash
scp ~/workspace/droidfarm/waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh
ssh root@209.222.10.93 'bash -n /usr/local/bin/df-watchdog.sh && echo SYNTAX_OK'
# no service restart needed — the minute-cron picks it up
```

## Verify on the VPS
- `bash -n /usr/local/bin/df-watchdog.sh` → clean.
- `grep -n "V16-NAME-GATE-START" /usr/local/bin/df-watchdog.sh` → gate present.
- With a healthy `instances.json`, the next cron cycles log no new WARN
  lines (valid names pass silently).
- The gate can be observed without touching real state: copy the script
  to /tmp, set `DF_INSTANCES="../evil rbx1"`, run it against a scratch
  `DF_ST`/`DF_LOG` — the log shows `skipping 1 invalid instance
  name(s): ../evil` and no marker file appears outside the scratch dir.

## Rollback
Restore `/usr/local/bin/df-watchdog.sh` from the timestamped backup the
apply-all run leaves next to it (or re-scp the previous version); no
restart needed.
