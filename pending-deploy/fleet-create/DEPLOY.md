# DEPLOY: fleet bulk-create ("Create fleet")

## What it is
One-tap creation of N instances named `<prefix><i>` (e.g. rbx21…rbx25) for
the 20-instance farm: dashboard fleet bar gains a "Create fleet" button that
opens a plan modal (prefix, count 1–20, RAM, CPUs, node), previews the exact
names plus a RAM/binder headroom check, then fires an async sequential-create
job with progress in the existing fleet progress modal.

- `POST /api/instances/fleet/plan` -> 200 {names, total, provider, headroom}
  (dry run, no side effects). headroom = {host_ram_mb, current_limits_mb,
  new_limits_mb, overcommit_mb, binder_free_triplets, warning}.
- `POST /api/instances/fleet` -> 202 {job_id, action:"create-fleet", total,
  names, provider}; polls via the existing `GET /api/instances/batch/<job_id>`.
- REFACTOR: single-create endpoint (`_create_instance`) now delegates to
  shared `create_local_instance()` / `create_remote_instance()` cores — the
  fleet worker calls the same functions, so single and fleet creates behave
  identically (409/500 codes, reservation rollback, binder self-heal).
- Headroom flags overprovisioning BEFORE creation: e.g. 5×2GB on the 31GB
  host warns "memory limits would exceed host RAM by N MB"; binder shortage
  warns with the self-heal capacity note. Warning shown in red in the plan
  and repeated in the confirm() dialog — never a silent overcommit.
- One running batch job at a time (409 otherwise, shared with the fleet
  action engine); per-instance failures recorded, never abort the rest.

## Files (ride apply-all from HEAD — no new stage files needed)
- manager/app.py -> /root/jerusalem/app.py
- manager/static/index.html -> /root/jerusalem/static/index.html

## Preferred deploy (from an SSH-capable session)
```bash
cd ~/workspace/droidfarm
bash pending-deploy/apply-all/deploy-all.sh root@209.222.10.93
```
Preflight runs this package's suite (t14) plus all other suites; on success it
scps the files, restarts jerusalem, and verifies.

## Standalone deploy (scp only these two files)
```bash
scp manager/app.py manager/static/index.html root@209.222.10.93:/root/jerusalem/
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && \
  systemctl restart jerusalem && sleep 3 && systemctl is-active jerusalem'
```

## Verify
1. Dashboard (logged in) shows "Create fleet" in the Fleet: bar
2. Open it, set prefix=rbx count=2, Preview plan -> names rbx21, rbx22 (or
   next free), RAM line, no/binder warnings as appropriate
3. Create -> fleet progress modal counts 0/2 -> 2/2 done, grid refreshes,
   two new instance cards appear
4. `GET /api/instances/batch/<job_id>` (dashboard token) ->
   {action:"create-fleet", status:"done", names:[...], failed:[]}

## Tests
`python3 pending-deploy/fleet-create/test_fleet_create.py` — 71/71 hermetic
checks: validation/sanitize/bounds, naming continuation + collision filter,
headroom math (overcommit/binder/remote/unknown-host), local core
(409/500/rollback/ip-override/binder-exhausted), remote core
(404/409-offline/passthrough), job lifecycle (202 shape/409 guard/worker),
failure modes (per-instance errors, exception safety, node-offline mid-job),
route placement, job-view names, dashboard contract.

## Safety notes
- Never touches instance data, credentials, or tokens; only creates new
  containers (docker run) and records them in instances.json.
- Names sanitized (`[^a-zA-Z0-9_-]`, 24-char prefix, 32-char names); no user
  input reaches a shell — binders/IPs are server-generated.
- Auth required on both fleet routes (dashboard token), same as other APIs.
- Sequential creation (one docker run at a time); auto-login watcher picks
  up new instances on their first boot as before.
