# thread-supervisor

Manager-side background-thread supervisor (`manager/app.py` only).

- `register_supervised(name, factory)` — registry of persistent background
  threads (`auto-login-watcher`, `resource-poller`).
- `thread_supervisor()` — daemon loop, every 30s: dead thread -> restart via
  factory; >3 restarts/hour -> `given_up` + one ALERT (no restart storm).
- `_sup_log()` — appends `WARN`/`ALERT` lines to the watchdog log so the
  dashboard "Eventos" modal surfaces restarts without any dashboard change.
- All helpers best-effort, never raise.

Tests: `test_thread_supervisor.py` — 36/36 (registration, start/no-op,
restart-on-death, circuit breaker + no ALERT spam, window expiry, raising
factory, log format, corrupt registry, main() wiring contract, constants,
loop resilience). Wired as apply-all preflight `t33`.
