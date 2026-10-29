# create-ip-gate — DEPLOY.md

## What
Fixes an authenticated shell-injection in the manager's instance-create
path. `POST /api/instances` passes `data.get("ip")` into
`create_local_instance()`, which interpolated it into a `shell=True`
`docker run` command line with no validation:

```json
{"name": "x1", "ip": "10.20.0.5;touch${IFS}/tmp/x", "cpus": 2, "memory_mb": 2048}
```

would have executed arbitrary commands as root on the manager host.
The route-name-gate work (2026-10-19) covered name captures but missed
the `ip` parameter — the only remaining user-controlled interpolation
into a manager shell string (cpus/memory are float/int-coerced at the
route; name is create-time-gated; binders/instance_id are internal).

## Fix
- `manager/app.py`: new `valid_ipv4()` (strict dotted-quad via stdlib
  `ipaddress.IPv4Address`; rejects hostnames, IPv6, CIDR, leading zeros,
  whitespace, and all shell metacharacters; never raises).
- `create_local_instance()` (shared by single-create and the fleet-create
  worker): invalid ip → `400 {"error": "invalid IP (dotted-quad IPv4 only)"`
  with the reservation rolled back, before any docker call.
  `next_free_ip()` only yields dotted quads, so the gate never fires on
  the generated-IP path.

## Deploy
Rides apply-all from HEAD — `manager/app.py` is already in the stage
list. No new stage files, no restart beyond the normal
`systemctl restart jerusalem` that apply-all performs.

Standalone (SSH-capable session):
```
scp manager/app.py root@<vps>:/root/jerusalem/app.py
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify
- Create with a hostile ip returns 400 and no container is created:
  `curl -sk -X POST https://<vps>:8080/api/instances -H "Authorization: Bearer $TOKEN" \
    -d '{"name":"t1","ip":"10.20.0.5;id","cpus":2,"memory_mb":2048}'`
  → `{"error": "invalid IP (dotted-quad IPv4 only)"}`, and
  `docker ps -a | grep wd-t1` is empty.
- Normal create (no ip / valid ip) still works: 200 with the instance in
  `/api/instances`.

## Tests
`python3 pending-deploy/create-ip-gate/test_create_ip_gate.py`
— hermetic real-app import; exact-count line `PASS=98 FAIL=0`.
