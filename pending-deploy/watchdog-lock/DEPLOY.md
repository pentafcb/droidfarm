# DEPLOY: watchdog-lock (df-watchdog v11 — single-instance lock)

Serializes the minute-cron watchdog runs with a `mkdir` mutex so a hung
`docker` CLI can never stack watchdog processes forever.

## What it deploys

- `waydroid-image/df-watchdog.sh` -> `/usr/local/bin/df-watchdog.sh`
  (v11: lock acquire before any docker call, stale-takeover after 600s,
  pid-checked EXIT-trap release).

Already rides the apply-all stage list from HEAD — no new stage files.
Preferred path: run `pending-deploy/apply-all/deploy-all.sh` from an
SSH-capable session (preflight runs this package's suite as t37).

## Standalone deploy (SSH-capable session only)

```bash
# from ~/workspace/droidfarm:
scp waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh
ssh root@209.222.10.93 'bash -n /usr/local/bin/df-watchdog.sh && echo OK'
```

No service restart needed — the minute-cron picks the new script up on its
next run. The lock activates on the first post-deploy run; a pre-existing
pile-up (if any) drains as the old lockless processes exit.

## Verify

```bash
ssh root@209.222.10.93 'sleep 75; ls -d /tmp/df-watchdog.lock 2>/dev/null || echo "no stale lock"; tail -5 /var/log/df-watchdog.log'
```

Expected: no `/tmp/df-watchdog.lock` left behind between runs, watchdog log
shows normal per-minute activity, at most one `ps aux | grep -c df-watchdog`
process at any moment.
