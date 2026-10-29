# DEPLOY: watchdog-v12 (df-watchdog v12 — manager probe requires a complete body)

The manager self-check (v5) only checked the HTTP status: a starved manager
answering 200 with a fast TTFB and then closing the connection mid-body
(curl exit 18) read as "healthy" forever, so the last-resort auto-recovery
never fired. The probe now fetches the body to a temp file and requires
BOTH curl exit 0 (complete transfer) and 2xx/3xx status; a 2xx/3xx with a
truncated body is a failed probe with a distinct "body truncated" log line,
on the same 2-strike restart logic.

## What it deploys

- `waydroid-image/df-watchdog.sh` -> `/usr/local/bin/df-watchdog.sh`
  (v12: probe fetches body to `$ST/mgr.probe.body`, requires curl exit 0
  + 2xx/3xx; truncated 2xx -> "manager HTTP <code> but body truncated
  (curl rc <rc>, strike N/2)"; temp files removed after every probe;
  degraded auto-clear only on a complete+healthy probe).

Already rides the apply-all stage list from HEAD — no new stage files.
Preferred path: run `pending-deploy/apply-all/deploy-all.sh` from an
SSH-capable session (preflight runs this package's suite as t38).

## Standalone deploy (SSH-capable session only)

```bash
# from ~/workspace/droidfarm:
scp waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh
ssh root@209.222.10.93 'bash -n /usr/local/bin/df-watchdog.sh && echo OK'
```

No service restart needed — the minute-cron picks the new script up on its
next run.

## Verify

```bash
ssh root@209.222.10.93 'grep -c "body truncated" /usr/local/bin/df-watchdog.sh'
# expect >= 2 (probe code + header comment)
# then watch one cron cycle:
ssh root@209.222.10.93 'sleep 65; tail -5 /var/log/df-watchdog.log'
# a healthy manager logs nothing new about the probe (silent pass)
```

34/34 hermetic checks: `bash pending-deploy/watchdog-v12/test_watchdog_v12.sh`
(fake curl honors `-o` and truncates with exit 18; fake docker/systemctl).
