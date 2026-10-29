# df-watchdog v12 — manager probe requires a complete body

## Problem
The v5 manager self-check probed `http://127.0.0.1:8080/` with
`curl -s -o /dev/null -w '%{http_code}'` — it discarded the body and only
checked the status line. A starved/thread-exhausted manager answers 200
with a fast TTFB and then closes the connection mid-body (curl exit 18),
which read as "healthy" forever: the last-resort auto-recovery (restart
the jerusalem service) never fired, and the DEGRADED circuit breaker never
tripped. This exact symptom (fast 200, mid-body close, curl exit 18 on 5 of
8 fetches) was observed against the live dashboard on 2026-10-11.

## Fix (waydroid-image/df-watchdog.sh, v12)
- The probe fetches the body to `$ST/mgr.probe.body` (instead of
  `/dev/null`) and captures both curl's exit code and the HTTP status.
- Healthy = curl exit 0 (complete transfer) AND 2xx/3xx status.
- A 2xx/3xx with a non-zero curl rc is a failed probe with a distinct log
  line: `manager HTTP <code> but body truncated (curl rc <rc>, strike N/2)`
  — the diagnosis is visible in the dashboard "Eventos" modal instead of
  being swallowed by a green check.
- Same 2-strike restart/start logic, same circuit-breaker budget
  (>2 restarts/hour -> `manager.degraded`, pauses auto-recovery).
- `manager.degraded` auto-clears only on a complete+healthy probe — a
  truncated 200 can no longer clear a degraded state.
- Temp files (`mgr.probe.body`, `mgr.probe.code`) are removed after every
  probe; a pre-existing file from a killed run is removed before the probe.
- False-positive risk is ~nil: the probe hits localhost and a healthy
  manager always completes the small page within the 8s cap.

## Tests
`test_watchdog_v12.sh` — 34/34 hermetic (real watchdog vs fake docker /
fake curl honoring `-o` with a truncation mode / fake systemctl):
complete-200 silent pass, truncated-200 strike 1 with distinct log line,
two truncations -> one restart + strike reset, truncated-then-complete ->
no restart, complete-500 keeps old wording, connect-timeout keeps old
wording, truncated probe never auto-clears degraded, truncation storm ->
circuit breaker trips with "manager service" reason and no log spam,
complete-302 healthy (v5 regression), curl-missing skip unchanged
(v5 regression). The v5 suite itself (27/27) still passes unmodified.
