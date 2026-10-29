# DEPLOY: stream-frameless-guard

Frameless-loop guards on the MJPEG + H264 streaming paths (15s, then the
server closes the loop; the dashboard reconnects / falls back on its own).

## What it deploys

- `manager/app.py` — `WS_FRAMELESS_TIMEOUT = 15` + guard in `_handle_ws`
- `manager/h264_stream.py` — `frameless_timeout` in `H264Broadcaster._run`

Both files already ride the apply-all stage list from HEAD — no new stage
files. Preferred path: run `pending-deploy/apply-all/deploy-all.sh` from an
SSH-capable session (preflight runs this package's suite as t30).

## Standalone deploy (SSH-capable session only)

```bash
# from ~/workspace/droidfarm:
scp manager/app.py manager/h264_stream.py root@209.222.10.93:/root/jerusalem/
ssh root@209.222.10.93 'cd /root/jerusalem && python3 -m py_compile app.py h264_stream.py && systemctl restart jerusalem'
```

## Verify

```bash
ssh root@209.222.10.93 'systemctl is-active jerusalem && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/'
journalctl -u jerusalem --since '5 min ago' | grep -c 'no frames for'  # 0 when healthy
```

Expected: service `active`, dashboard HTTP 200. Open a viewer on a
stopped instance's stream (or block an instance's screencap loop) and
confirm one `[ws] mjpeg <name>: no frames for 15s` line appears in the
journal, then frames resume on dashboard reconnect.

## Rollback

Redeploy the previous `app.py` + `h264_stream.py` from the apply-all
backup (`/root/jerusalem-deploy/backups/`) and restart jerusalem.

## Tests

```bash
python3 pending-deploy/stream-frameless-guard/test_stream_frameless_guard.py  # 24/24
python3 pending-deploy/h264-mse/test_h264_integration.py  # 59/59 regression
```
