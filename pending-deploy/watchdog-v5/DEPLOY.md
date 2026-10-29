# DEPLOY — df-watchdog v5: manager service self-check

## What
`waydroid-image/df-watchdog.sh` v5 adds a manager self-check to the
minute-cron: probes `http://127.0.0.1:8080/` (2xx/3xx = healthy). A
wedged-but-alive manager (deadlock, thread exhaustion) is invisible to systemd,
so 2 consecutive failed probes trigger `systemctl start jerusalem` (service
down) or `systemctl restart jerusalem` (service up but not answering), counted
in the v4 circuit-breaker budget (>2 manager restarts/hour ->
`/tmp/df-watchdog/manager.degraded` with the reason on its first line,
auto-recovery pauses until a probe passes again, which auto-clears it).
Missing curl degrades to one WARN line per day, never silent. Instance-level
recovery is unaffected by a manager-degraded state, and the v4 behavior for
instances is unchanged (v4 suite still 26/26 against the v5 script).

## Stage status
No new stage files — `df-watchdog.sh` is already in the apply-all stage list
and deploys from repo HEAD. This package exists for the test suite and docs.

## Standalone deploy (no SSH session handy / manual)
```bash
scp ~/workspace/droidfarm/waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh
ssh root@209.222.10.93 "bash -n /usr/local/bin/df-watchdog.sh && echo SYNTAX_OK"
# no service restart needed — the minute-cron picks it up automatically
```
Verify (wait ~2 min, then):
```bash
ssh root@209.222.10.93 "tail -20 /var/log/df-watchdog.log"
# expect: no new manager lines on a healthy box; the probe is silent when 200
ssh root@209.222.10.93 "curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://127.0.0.1:8080/"
# expect: 200
```

## Tests
`bash pending-deploy/watchdog-v5/test_watchdog_v5.sh` — 27/27 pass (fake
docker/curl/systemctl): healthy no-op, 2-strike restart vs start branching,
302-healthy, storm -> DEGRADED (reason names manager service, no log spam),
auto-clear, curl-missing per-day WARN, failed-restart logged, instance
recovery unaffected by manager-degraded.

## Notes
- Restart cost: active viewer streams drop on a manager restart — bounded by
  the 2-strikes rule + 2/hour circuit breaker + DEGRADED pause.
- During a deploy's own restart the cron may log one "strike 1/2" line and
  then clear it — harmless.
