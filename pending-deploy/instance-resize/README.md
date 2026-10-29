# instance-resize

Live per-instance RAM/CPU resize. `docker update` applies new
`--memory`/`--cpus` limits to a running (or stopped) container with no
restart.

- Manager: `validate_resize_params()` + `resize_local_instance()` +
  `resize_remote_instance()` + `POST /api/instances/<name>/resize`
  (auth-gated, route-name-gated). A memory shrink below live usage is
  refused (400); unknown usage fails open. `instances.json` is written
  only after docker accepts the limits.
- Wing agent 1.0.31: `resize_instance_linux()` (list-form docker),
  `resize_usage_ok()` (agent's own cached stats),
  `POST /api/agent/instances/<name>/resize` (agent-key authed,
  name-gated, 501 on Windows).
- Dashboard: per-card "Recursos" button + modal (RAM MB, CPUs),
  prefilled, Spanish, inline errors.

Tests: `test_instance_resize.py` — 77/77 hermetic (real app/agent
imports, faked docker/instances/wing/usage; dashboard block driven in
node). Preflight t100.
