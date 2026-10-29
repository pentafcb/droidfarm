# wing-selfwatch-dashboard

## Why
The dashboard "Eventos" modal showed only the main node's watchdog log.
The wing agent's selfwatch thread (1.0.3+, every 60s) recovers remote
containers/helpers/streams and writes every recovery into its own
`selfwatch.log` — invisible to Penta unless he SSH'd into vultr.

## Design
- One new agent endpoint, no new daemon: `read_selfwatch_events()` uses
  the same tail discipline as the manager's `read_watchdog_events()`
  (64KB window, partial-first-line drop, 500-char cap).
- Lines are returned RAW from the agent; the manager applies its single
  canonical `watchdog_event_severity()` so main + node events render with
  identical tagging in one modal.
- The merge is chronological by the shared `[YYYY-MM-DD HH:MM:SS]` log
  prefix; unparseable lines trail (stable sort). Combined list truncated
  to the requested `lines` (most recent N).
- Fetch is fail-fast (`attempts=1`, 5s timeout): the modal re-polls every
  15s, so retries would only add latency. Offline/stale-heartbeated nodes
  are skipped; old agents (404) contribute nothing silently.
- All helpers best-effort, never raise — a node-fetch bug degrades to the
  old local-only modal, never a broken response.
- No API/dashboard breakage for older clients: `nodes` and per-event
  `node` are additive fields.

## Files touched
- `wing/agent.py` (VERSION 1.0.9)
- `manager/app.py`
- `manager/static/index.html`
