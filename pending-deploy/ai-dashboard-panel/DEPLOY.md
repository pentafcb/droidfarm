# Deploy: dashboard IA panel + AI console log

Ships: manager/app.py (AI key storage + agent control + log endpoints),
manager/static/index.html (IA button + modal + console, Spanish, no
emojis), ai-player/ai_player.py (Bearer-token auth, status file,
request/response console log).

## Deploy commands (run from ~/workspace/droidfarm)

```bash
scp -r ai-player/ root@64.177.49.68:/root/jerusalem/ai-player/
scp manager/app.py root@64.177.49.68:/root/jerusalem/manager/app.py
scp manager/static/index.html root@64.177.49.68:/root/jerusalem/manager/static/index.html
ssh root@64.177.49.68 'systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify

- `systemctl is-active jerusalem` → `active`
- Dashboard (after panel login): fleet bar shows **IA**; the IA modal
  opens, shows "Sin clave guardada" before any key is saved.
- Paste a key → Guardar → "Clave guardada"; Probar → "Clave válida".
- Start on a test instance → Registro de la IA shows PETICIÓN /
  RESPUESTA lines every step; Detener stops the agent.
- On the box: `/root/jerusalem/ai_config.json` is mode 600;
  `/root/jerusalem/ai_calls.jsonl` grows one JSON entry per AI
  request/response. No key is ever returned by any API route
  (GET /api/ai/status carries only configured/running/step/cost).

## Notes

- The key lives in exactly one server-side file (ai_config.json, 600,
  atomic tmp+rename, swept at startup like tokens.json). The user never
  touches a file: everything goes through the password-protected
  dashboard panel.
- The agent child is spawned by the manager with the key + a fresh
  24h Bearer token in memory only. If the manager restarts, a running
  agent is re-detected via its status-file pid.
- Local verification done before staging: py_compile clean, agent
  selftest 9/9, dashboard JS node --check clean, key round-trip +
  600 perms + no-leak status + log-tail tests green (import harness,
  temp data dir, no agent spawned).
