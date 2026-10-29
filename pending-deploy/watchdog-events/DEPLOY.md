# Deploy: watchdog event feed (dashboard "Eventos" modal)

The watchdog already recovers from malfunctions automatically, but Penta
watches the dashboard, not SSH — nobody could see WHAT the watchdog did
overnight (recoveries, DEGRADED trips, drift alerts, manager restarts).
The fleet bar now has an "Eventos" button that opens a modal with the last
100 watchdog log lines, severity-tagged (alert red / warn amber / ok green),
auto-refreshing every 15s while open.

## What rides apply-all

No new stage files: `manager/app.py` and `manager/static/index.html` are
already in the apply-all stage list, and this rides it from repo HEAD.
Suite `t31` in preflight:
`pending-deploy/watchdog-events/test_watchdog_events.py` (37/37).

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH:
scp ~/workspace/droidfarm/manager/app.py \
    root@209.222.10.93:/root/jerusalem/app.py
scp ~/workspace/droidfarm/manager/static/index.html \
    root@209.222.10.93:/root/jerusalem/static/index.html
ssh root@209.222.10.93 \
  'python3 -m py_compile /root/jerusalem/app.py && \
   systemctl restart jerusalem && \
   systemctl is-active jerusalem'
```

## Verify

```bash
# authed probe: POST /api/login first, then
curl -s -H "Authorization: Bearer $TOK" \
  'http://127.0.0.1:8080/api/watchdog-events?lines=5' | head -c 400
# expect {"ok": true, "path": "/var/log/df-watchdog.log", "events": [...]}
# unauthenticated:
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/watchdog-events
# expect 401
```

Then open the dashboard: the fleet bar shows "Eventos"; the modal lists
recent watchdog lines with colors and refreshes every 15s. No restart is
needed for a dashboard-only check of the button/modal (served from disk),
but the new API route needs the `systemctl restart jerusalem` above.

## Security notes

- The log path is FIXED (`/var/log/df-watchdog.log`, overridable only via
  the `DF_WATCHDOG_LOG` env var on the server) — never taken from user
  input, so no path traversal.
- `lines` is clamped to [1, 200]; only the last 64KB of the file is read,
  so a huge log can't spike memory.
- Lines are capped at 500 chars and rendered with `textContent` (never
  `innerHTML`), so log content can't inject HTML/JS.
- The watchdog log never contains credentials, tokens, or passwords (the
  watchdog only logs instance names, counts, and reasons) — safe to expose
  behind the existing dashboard Bearer auth.
- Endpoint is auth-gated like every other `/api/*` route (401 without a
  valid dashboard token).
