# DEPLOY: watchdog-timeout (df-watchdog v13 — bounded docker calls)

Every `docker` call in the minute-cron watchdog is now `timeout`-bounded via
the `dw()` wrapper, so a hung docker (wedged daemon / wedged waydroid
session) can no longer freeze the whole run — and with it the manager
self-check, the last-resort auto-recovery. A timed-out probe is UNKNOWN,
not dead: no false restarts, no burned circuit budgets, one WARN per
instance per day.

## What it deploys

- `waydroid-image/df-watchdog.sh` -> `/usr/local/bin/df-watchdog.sh`
  (v13: `dw()` wrapper + 11 bounded call sites + unknown-vs-dead probe
  semantics + one-per-day timeout WARNs; the two backgrounded helper
  restarts stay bare `docker` by design).

Already rides the apply-all stage list from HEAD — no new stage files.
Preferred path: run `pending-deploy/apply-all/deploy-all.sh` from an
SSH-capable session (preflight runs this package's suite as t92).

## Standalone deploy (SSH-capable session only)

```bash
# from ~/workspace/droidfarm:
scp waydroid-image/df-watchdog.sh root@64.177.49.68:/usr/local/bin/df-watchdog.sh
ssh root@64.177.49.68 'bash -n /usr/local/bin/df-watchdog.sh && echo OK'
```

No service restart needed — the minute-cron picks the new script up on its
next run. (Also scp to the old VPS 209.222.10.93 if it is still live.)

## Verify

```bash
ssh root@64.177.49.68 'grep -c "v13" /usr/local/bin/df-watchdog.sh'
# expect >= 3 (header block + wrapper + call-site comments)
# then watch one cron cycle:
ssh root@64.177.49.68 'sleep 65; tail -5 /var/log/df-watchdog.log'
# expect the normal per-minute quiet (no new WARNs on a healthy box)
```
