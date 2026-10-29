# agent-ip-gate — DEPLOY.md

## What
Agent-side parity with the manager's create-ip-gate. `POST
/api/agent/instances` passed `data.get("ip")` into
`create_instance_linux()`, which handed it verbatim to `docker run --ip`
with NO validation — the instance name was fullmatch-gated, the ip was
not. The agent's `docker()` is list-form (no shell), so a hostile ip
could never inject a command here, but it failed cryptically deep in
`docker run`, and any future shell-form sink would have inherited the
hole. The only caller today is the manager's remote-create path, which
always sends a generated `10.20.0.N` quad — so this changes no
working behavior, it hardens the boundary.

## Fix
- `wing/agent.py`: new `valid_agent_ipv4()` (strict dotted-quad via
  stdlib `ipaddress.IPv4Address`; rejects hostnames, IPv6, CIDR,
  leading zeros, whitespace, and all shell metacharacters; never
  raises). Deliberately stricter than the manager's `valid_ipv4`
  (which strips): the agent passes ip verbatim into the docker argv,
  so the validated value must be exactly the value docker receives.
- `create_instance_linux()`: invalid ip → `(False, "invalid ip")`
  before any docker call (same precedence shape as the existing
  "invalid name" gate, which still fires first).
- NO agent version bump: valid-input behavior is unchanged, so the
  manager's login gate (>= 1.0.38) and every VERSION-membership suite
  are unaffected. The fix rides apply-all from HEAD and the wing
  self-update path pushes it on the next heartbeat after deploy.

## Deploy
Rides apply-all from HEAD — `wing/agent.py` is already in the stage
list. No new stage files, no restart beyond the normal
`systemctl restart jerusalem` that apply-all performs. After the
manager deploy, the vultr node self-updates to the gated agent on its
next heartbeat (no installer re-run needed).

Standalone (SSH-capable session, manager host):
```
scp wing/agent.py root@<vps>:/root/jerusalem/wing/agent.py
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/wing/agent.py && systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify
- A direct agent create with a hostile ip fails cleanly and creates
  nothing (agent port, key-gated):
  `curl -sk -X POST http://<node>:8090/api/agent/instances -H "X-Agent-Key: $KEY" \
    -d '{"name":"t1","cpus":2,"memory_mb":2048,"ip":"10.20.0.5;id"}'`
  → `{"ok": false, "msg": "invalid ip"}`, and
  `docker ps -a | grep wd-t1` is empty.
- Normal remote create from the dashboard still works: 200 with the
  instance in `/api/instances`.

## Tests
`python3 pending-deploy/agent-ip-gate/test_agent_ip_gate.py`
— hermetic real-agent import; exact-count line `PASS=66 FAIL=0`.
