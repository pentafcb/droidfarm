# Deploy the AI screen-touch agent + dashboard IA panel to the VPS

Target: the Jerusalem box (the one running `jerusalem.service`). The
manager spawns the agent itself, so there is no separate service and no
env file for you to fill in: copy the files, restart the manager, then
do everything from the dashboard IA panel.

## 1. Copy the files

From the repo (or any SSH-capable session):

```bash
scp -r ai-player/ root@<vps-ip>:/root/jerusalem/ai-player/
scp manager/app.py root@<vps-ip>:/root/jerusalem/manager/app.py
scp manager/static/index.html root@<vps-ip>:/root/jerusalem/manager/static/index.html
```

## 2. Restart the manager

```bash
systemctl restart jerusalem
systemctl is-active jerusalem
```

Zero instances need to be stopped for this: the change adds new API
routes and a dashboard panel; it does not touch instance state.

## 3. Use it from the dashboard (no files)

1. Open the dashboard, log in with the panel password, tap **IA**.
2. Paste the nano-gpt key, tap **Guardar clave**, then **Probar**.
3. Type the instance (e.g. `srt1`) and the Roblox goal, tap
   **Iniciar agente**.
4. Watch the **Registro de la IA** console in the same panel: every AI
   request and response appears there, refreshed every 5 seconds.
5. Tap **Detener** any time to hand the screen back to your phone.

## 4. Verify on the box

- `systemctl is-active jerusalem` → `active`
- `ls -l /root/jerusalem/ai_config.json` → `-rw-------` (600), only
  after a key is saved from the panel
- `tail /root/jerusalem/ai_calls.jsonl` → one JSON entry per AI
  request/response while the agent runs
- Dashboard IA panel shows "Clave guardada" and the agent state

## Manual run (optional, without the dashboard)

```bash
cd /root/jerusalem/ai-player
export NANO_GPT_API_KEY="..."              # env only, never in a file
export JERUSALEM_DASHBOARD_PASSWORD="..."  # dashboard login password
export INSTANCE="srt1"
export GOAL="Open Roblox and join Bloxburg"
python3 ai_player.py --once --dry-run   # see one action without touching
python3 ai_player.py --once            # one real step
python3 ai_player.py                  # full loop
```
