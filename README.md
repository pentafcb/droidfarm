# Jerusalem 1.0

Multi-instance Waydroid Android farm for running multiple Roblox accounts simultaneously, managed from a web dashboard with live screen streaming.

## Architecture

- **Main node** (`manager/`): Python web dashboard + API server. Creates and manages Waydroid containers via Docker, one Android instance per container. Handles streaming, input, Roblox auto-install, and per-instance accounts.
- **Wing nodes** (`wing/`): Lightweight agent (`agent.py`) that runs on remote machines. Registers with the main node via a static join token, creates local Waydroid containers on demand.
- **Android image** (`waydroid-image/`): Docker image with Waydroid, entrypoint, watchdog, and Roblox keepalive. One container = one isolated Android instance.

## Components

### manager/
- `app.py` — Dashboard backend + REST API (instances, streaming, input, accounts, nodes)
- `static/index.html` — Dashboard UI (live screenshots, WebRTC/MJPEG streaming, fleet controls)
- `h264_stream.py` — H.264-over-WebSocket streaming pipeline
- `jerusalem.service` — systemd unit for the main node
- `start.sh` — Local dev start script

### wing/
- `agent.py` — Wing node agent (HTTP API on 8090-8099, Docker management, self-watchdog)
- `install-wing.sh` — Linux one-command installer
- `install-wing.bat` — Windows installer

### waydroid-image/
- `Dockerfile` — Ubuntu 22.04 + Waydroid + Android tools
- `entrypoint.sh` — Container init (binder devices, network bridge, Waydroid boot, Roblox install)
- `df-watchdog.sh` — Host watchdog (screencap loops, disk pressure, memory, version-pin guard)
- `roblox-keepalive.sh` — In-Android keepalive (forces Roblox to stay foregrounded)

## Requirements

- Host kernel with binder/ashmem support (Waydroid), Docker, Python 3.10+.
- Optional for H.264 streaming: `aiortc` + `numpy` (falls back to MJPEG over WebSocket without them).
- Port 8080 (dashboard/API) and 8090–8099 (wing agent) reachable.

## Build the Instance Image

The image needs the pinned Roblox APKs in the build context — they are **not** committed (kept out by `.gitignore`).

1. Copy the two files from the pinned build (2.740.931, last x86_64 build — ARM-only builds fail on Waydroid):
   - `roblox-base.apk`
   - `roblox-x86_64.apk`
2. Place both next to `waydroid-image/Dockerfile`.
3. `cd waydroid-image && docker build -t waydroid-instance:latest .`

Without the APKs the `COPY` steps fail; with them the entrypoint installs Roblox automatically on first boot.

## Run the Manager

```bash
export JERUSALEM_PASSWORD='<your dashboard password>'   # default is 'changeme' — change it
export JERUSALEM_PORT=8080                                 # optional
export JERUSALEM_DATA=/root/jerusalem                     # optional, runtime data dir
./manager/start.sh
# or as a service:
cp manager/jerusalem.service /etc/systemd/system/
systemctl enable --now jerusalem
```

Open the dashboard at `http://<host>:8080`, sign in, then **Nodes → + Add node** for the one-line installers (the static join token is injected by the server — never committed).

## Join a Node

### Linux Node
Run the one-command installer from the dashboard (Nodes → Add node), needs root:
```bash
curl -sSL -H "X-Join-Token: <token>" http://<main-ip>:8080/api/install/install-wing.sh | sudo bash
```

### Windows Node
Run in PowerShell (from dashboard Nodes → Add node), needs elevation. The installer verifies the download before executing:
```powershell
powershell -WindowStyle Hidden -Command "curl.exe -sSL --fail -H 'X-Join-Token: <token>' http://<main-ip>:8080/api/install/install-wing.bat -o $env:TEMP\wing.bat; if ((Get-Content $env:TEMP\wing.bat -TotalCount 1 -ErrorAction SilentlyContinue) -eq '@echo off') { Start-Process $env:TEMP\wing.bat -WindowStyle Hidden } else { Write-Host 'ERROR: download failed - check token'; pause }; exit"
```

## API Overview

- `POST /api/login` — Dashboard auth (rate-limited: 10 fails/min/IP → 429)
- `GET/POST /api/instances` — List/create instances
- `GET /api/instances/{name}/screenshot` — Live screenshot
- `POST /api/instances/{name}/input` — Touch input (x, y, action)
- `GET/POST /api/instances/{name}/roblox-account` — Per-instance credentials (password never returned)
- `POST /api/instances/{name}/roblox-login` — Trigger Roblox auto-login
- `POST /api/providers/register` — Wing node enrollment (join token required)
- `GET /api/providers` — List nodes with stats

## Instance Management

Each instance is an isolated Waydroid container:
- **Resources**: 2 CPU, 2048 MB RAM (configurable per instance)
- **Display**: 1024×608 landscape
- **Network**: Unique 10.20.0.x IP + per-instance bridge (192.168.x.x)
- **Binder**: 3 binderfs devices per instance (auto-provisioned)
- **Roblox**: Auto-installed on first boot, pinned to 2.740.931, kept foregrounded by keepalive

## Security Notes

- Runtime secrets live in `JERUSALEM_DATA`, never in this repo: the join token is generated on first run, Roblox account passwords stay server-side and are never returned by the API.
- The install scripts in `wing/` contain `__MAIN_URL__` / `__JOIN_TOKEN__` placeholders — the server substitutes them when serving. Don't commit filled-in copies.
- Keep this repo **private**.
- Request bodies capped at 2MB, screenshots at 20MB, node enrollment rate-limited.
- Login rate-limited (10 failed attempts/min/IP).

## Deploy the Staged Queue

Each `pending-deploy/<package>/` has a `DEPLOY.md` and tests. To apply everything at once:

```bash
bash pending-deploy/apply-all/deploy-all.sh   # see APPLY.md first — backs up and can roll back
```

## Repo Layout

```
manager/          dashboard backend (app.py), H.264 stream helper, dashboard UI (static/), service unit, start script
wing/             node agent (agent.py) + Linux/Windows installers
waydroid-image/   instance image: Dockerfile, entrypoint, keepalive + watchdog scripts
pending-deploy/   staged upgrades with DEPLOY.md + tests each (apply-all/ applies them in order)
docs/             operator guide
```

## License

Private. All rights reserved.
