# remote-input-b64 — DEPLOY.md

## What
Fixes silent password mangling on remote-node Roblox logins. The wing
agent's `/input` endpoint ran base64-decoded text (`b64:true` — used ONLY
for the remote Roblox login password, sent by
`manager/app.py::_roblox_login_remote`) through `sanitize_input_text`,
which strips every character outside `[a-zA-Z0-9%s.,!?@_-]`. A password
containing `#$&*()+=/:;<>[]{}|\'"`~^` etc. was silently mangled, so the
login failed with a generic "login failed" while the SAME password worked
on local instances (the manager types the full password there via
`pwd.txt` + `input text "$P"`).

The b64 flag was designed for "shell-special characters" but the
sanitize defeated it — local/remote parity bug with no visible cause on
the dashboard.

## Fix
- `wing/agent.py` 1.0.37 → **1.0.38**:
  - New `_stage_b64_input_text()`: writes the decoded text to
    `input-b64.txt` in the instance's data volume (600, atomic
    tmp+rename) and returns the FIFO-safe command
    `input text "$(cat /data/media/0/jerusalem/input-b64.txt)"`.
    The secret bytes never appear in the command line; inside double
    quotes `$(...)` expansion results are not re-parsed, so `$`,
    backticks, quotes, `;`, newlines are inert data — exactly as safe as
    the manager's local pwd.txt approach, with identical `input text`
    semantics. The staged file is deleted ~60s later by a daemon thread,
    and only when its content still matches (a newer stage is never
    removed early).
  - The `/input` text branch now delegates to `_agent_text_cmd()`:
    b64 → staged file (staging failure → 500 "could not stage text
    input", never a silently mangled password); plain text keeps the
    sanitize-strip unchanged.
- `manager/app.py`: `_roblox_login_remote` now requires agent >= 1.0.38
  (was >= 1.0.2). Older agents refuse loudly ("node agent too old")
  instead of mangling silently; the fixed wing-update-path self-update
  pushes 1.0.38 to the node on the next heartbeat after deploy.

## Deploy
Rides apply-all from HEAD — `wing/agent.py` + `manager/app.py` are
already in the stage list. No new stage files. After the manager
restarts, the vultr node self-updates to 1.0.38 on its next heartbeat
(no installer re-run needed); until then remote logins report the
"too old" message honestly.

Standalone (SSH-capable session):
```
scp wing/agent.py root@<vps>:/root/jerusalem/wing/agent.py
scp manager/app.py root@<vps>:/root/jerusalem/app.py
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/wing/agent.py /root/jerusalem/app.py && systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify
- `python3 pending-deploy/remote-input-b64/test_remote_input_b64.py`
  → `44 passed, 0 failed`.
- After deploy + node self-update: `/api/agent/health` on the node
  reports `"version": "1.0.38"`.
- Remote login with a special-char password (e.g. containing `#`/`$`/`&`)
  records `ok:true` in `/api/instances/<n>/roblox-login-result`
  (previously `ok:false` with no useful reason).

## Tests
`python3 pending-deploy/remote-input-b64/test_remote_input_b64.py`
— hermetic real-agent import; exact-count line `44 passed, 0 failed`.
