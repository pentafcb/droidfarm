# Deploy: manager background-thread supervisor

`df-watchdog.sh` v5 restarts the whole manager service when it stops
answering HTTP, but a single dead background thread (the auto-login watcher
or the resource poller) inside an otherwise healthy service was invisible
and unrecoverable short of a full `systemctl restart jerusalem` — which
drops every connected viewer. The manager now supervises its own background
threads.

## What it does

- `manager/app.py`: `register_supervised(name, factory)` registry +
  `thread_supervisor()` daemon loop (every 30s). A dead registered thread is
  restarted via its factory; restarts are counted in a 1-hour sliding window;
  more than 3 restarts/hour marks the thread `given_up` and writes one ALERT
  (no spam) — a wedged thread then needs a human, surfaced instead of
  spin-restarting forever.
- Both persistent watchers (`auto-login-watcher`, `resource-poller`) are
  registered in `main()` instead of being started directly; the supervisor's
  first pass starts them, so startup behavior is unchanged.
- Every restart, failed restart, and give-up is appended to the watchdog log
  (`DF_WATCHDOG_LOG`, default `/var/log/df-watchdog.log`) with `WARN`/`ALERT`
  severity — the dashboard "Eventos" modal shows them with no changes.
- Everything is best-effort and never raises: a supervisor bug can never
  break the manager (worst case degrades to the old behavior — v5's
  service-level restart still covers a truly wedged manager).

## What rides apply-all

No new stage files: `manager/app.py` is already in the apply-all stage list,
and the supervisor rides it from repo HEAD. Suite `t33` in preflight:
`pending-deploy/thread-supervisor/test_thread_supervisor.py` (36/36).

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH:
scp ~/workspace/droidfarm/manager/app.py \
    root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 \
  'python3 -m py_compile /root/jerusalem/app.py && \
   systemctl restart jerusalem'
```

## Verify

- `systemctl is-active jerusalem` -> `active`
- `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/` -> `200`
- `journalctl -u jerusalem --since '2 min ago' | grep -i supervisor` shows
  the two initial `WARN supervisor started background thread ...` lines
- Dashboard "Eventos" modal shows those two lines (severity: amber WARN)
- Kill-test (optional, SSH): `kill` is not applicable to threads; instead
  confirm no duplicate starts over 10 minutes (log shows no repeat WARNs
  while the threads are healthy)
