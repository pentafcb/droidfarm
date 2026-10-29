# watchdog-heartbeat-banner — README

## Why
Every farm-wide auto-recovery pause so far surfaced as a dashboard
banner entry — except the pause that matters most: the watchdog itself
not running. Marker files can only report what the watchdog saw while
alive; a dead minute-cron leaves no marker, so the dashboard showed
green while nothing was being watched.

## How it works
- `manager/app.py`: `WATCHDOG_HEARTBEAT_MAX_AGE = 600`.
  `global_watchdog_health()` appends a trailing `{"key": "watchdog"}`
  entry when the state dir exists but `heartbeat` is missing ("never
  fired") or older than 600s ("not firing, heartbeat Ns ago"). Trips
  first, meta-state last — the marker ordering contract is unchanged.
  A missing state dir still returns `[]` (fresh install is
  indistinguishable there).
- `manager/static/index.html`: `GLOBAL_HEALTH_LABELS` gains
  `watchdog:'Watchdog'`; the existing banner renderer shows it with
  the reason in the tooltip. No new UI code.
- No watchdog/agent change: the heartbeat touch already exists
  (watchdog-liveness), and a dead wing agent is already covered by the
  node-offline badge.

## Deliberate boundaries
- 600s, not verify-live's 300s: a persistent banner must not flash on
  one slow run; it must catch a dead cron (10 missed minute-runs).
- The entry is the liveness state, not a latched alarm: it clears on
  the next refresh after a fresh heartbeat, same as the other banner
  entries clear when their markers clear.
- A run blocked by another run's lock never touches the heartbeat, so
  a wedged lock-holder also trips the banner — that is the intended
  signal, not a false positive.
