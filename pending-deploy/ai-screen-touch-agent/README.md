# Jerusalem AI screen-touch agent

An AI that plays Roblox by *looking* at an instance's screen and *touching*
it — through the Jerusalem manager you already have. No new driver, no
emulator hack: it reuses the dashboard's own screenshot frame and input API.

## How it works

```
Waydroid instance ──live.png──> agent ──JPEG + goal──> nano-gpt (GLM 5.3 Uncensored, vision)
     ^                                                              │
     │                                          JSON action (tap/swipe/key/wait/done/need_help)
     └────── manager POST /api/instances/<name>/input <──────────────┘
```

1. **See.** The agent reads the instance's `live.png` frame from the docker
   volume (the exact frame the dashboard viewer shows) and downscales it
   to 480px wide JPEG quality 60 — the same small frame the dashboard
   already uses (~6–30 KB).
2. **Think.** It sends that frame + your goal to nano-gpt with a system
   prompt that explains the touch system: the screen is 1024×608, you
   answer in per-mille coordinates (0–1000 per axis, 500/500 = center),
   and you reply with strict JSON only. No history is carried — each call
   is stateless except the previous action, so cost never grows.
3. **Touch.** The model returns one action. The agent validates it
   (known type, coordinates in range, known key) and executes it via
   `POST /api/instances/<name>/input` with a Bearer token from
   `POST /api/login` (dashboard password). Invalid replies are dropped,
   never executed.
4. **Repeat** every few seconds until the model says `done` — or it gets
   stuck, and the phone takes over (below).

## The prompt the model gets

The system prompt is the file `SYSTEM_PROMPT.md` in spirit and the
`SYSTEM_PROMPT` constant in `ai_player.py`. In short, it tells the model:

- You are driving an Android screen. You see screenshots, you can touch.
- Coordinates are per-mille, never pixels.
- Output strict JSON only: `tap`, `swipe`, `key` (HOME/BACK/APP_SWITCH/
  POWER/VOLUME_UP/VOLUME_DOWN), `wait`, `done`, `need_help`.
- Tap only what is actually visible. If loading, wait. If stuck, or asked
  to log in / pay / enter a password, say `need_help`. Never enter
  credentials.

## Token optimization

| Trick | Effect |
|---|---|
| 480px JPEG q60 frames | vision tokens stay small (~1k/step) |
| Stateless calls (no history) | cost per step never grows |
| Send-on-change | identical frame = no API call = $0 for static screens |
| JSON-only, max_tokens 200, temp 0.2 | no billed reasoning prose |

Verified model (2026-10-28, nano-gpt catalog):
`z-ai/glm-5.3-flash-uncensored` — vision on, text+image in, text out,
1M context, $0.20/1M input tokens, $0.80/1M output tokens.
Typical step ≈ 1k image + 300 prompt + 50 output tokens ≈ **$0.0003**.
100 steps ≈ a few cents. Set `NANO_GPT_MODEL` to use another slug.

## Phone takeover (problems → you)

The agent **never taps blindly**. It stops and writes `ai-takeover.json`
(reason + last action + timestamp) when:

- the model says `need_help` (stuck, login screen, payment screen),
- 3 invalid model replies in a row,
- 3 consecutive input failures,
- 10 steps with zero screen change (wedged game/menu).

When that happens, open the Jerusalem dashboard on your phone and drive
the instance yourself. Restart the agent when you want it to resume.

## Dashboard panel (no files for you)

Open the Jerusalem dashboard, log in with the panel password, and tap
**IA** in the fleet bar. Everything happens there:

- **Clave de nano-gpt:** paste the key, Guardar. It is stored on the
  server only (600-permission file, atomic write), behind the dashboard
  login, and it is never sent back to the browser — the panel only ever
  shows "Clave guardada" or "Sin clave guardada". Probar checks the key
  against nano-gpt; Borrar removes it (and stops the agent).
- **Instancia + Objetivo:** pick the instance and describe the Roblox
  goal, then Iniciar agente / Detener. The manager spawns the agent
  itself and hands it the key + a fresh dashboard token in memory — no
  env file, no password typing.
- **Registro de la IA:** the console box in the same panel logs every
  AI request and response (PETICIÓN / RESPUESTA / ACCIÓN / ERROR lines
  with step, tokens, and cost), refreshed every 5 seconds while open.
  Request lines carry metadata only — never image bytes, keys, or
  passwords.

## Run it (manual, without the dashboard)

On the VPS host (where the docker volumes live):

```bash
export NANO_GPT_API_KEY="..."              # nano-gpt key, env only, never in a file
export JERUSALEM_DASHBOARD_PASSWORD="..."  # dashboard login password
export INSTANCE="srt1"
export GOAL="Open Roblox and join Bloxburg"
python3 ai_player.py --once     # single step, for testing
python3 ai_player.py            # full loop (default 300 steps, 3s apart)
python3 ai_player.py --dry-run  # log actions without touching anything
python3 ai_player.py --selftest # offline checks, no key needed
```

Env knobs: `NANO_GPT_MODEL`, `NANO_GPT_BASE_URL`,
`JERUSALEM_URL` (default `http://127.0.0.1:8080`), `JERUSALEM_TOKEN`
(minted by the dashboard; wins over the password login), `POLL_SECS`,
`MAX_STEPS`, `SHOT_PATH` (test with a local image), `TAKEOVER_FILE`,
`AI_STATUS_FILE`, `AI_LOG_FILE` (dashboard console log, JSONL).

## Security

- Keys and passwords come **only** from environment variables. This code
  never writes them to disk, logs, or output. For a persistent service,
  put them in a systemd `EnvironmentFile` with `chmod 600` (see DEPLOY.md).
- The model never sees any password. If a key was ever pasted in chat,
  rotate it in the nano-gpt portal — chat transcripts are not a vault.
- The agent does not log into Roblox and never types credentials. The
  instance must already be logged in (or use Jerusalem's existing
  auto-login). It only plays by sight and taps.

## Limits (honest)

- v1 drives **local** instances (volume screenshot + manager API). Wing
  nodes need the agent screenshot route — follow-up, same loop.
- It plays by sight, not by game knowledge: it can open Roblox, tap
  menus, and follow a goal you describe, but it is not a Roblox bot with
  built-in game strategy.
- DRM / login / payment screens always trigger takeover, by design.
