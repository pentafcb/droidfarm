# Deploy: push-script timeout hardening

The four `finish-deploy.sh` push scripts (`push-keepalive.sh`,
`push-booted-watcher.sh`, `push-screencap.sh`, `push-optimizer.sh`) used to
issue unbounded `docker` calls — a wedged daemon or wedged Waydroid session
hung the interactive post-deploy run forever at the first stuck instance
with no progress indication.

Now every docker call goes through a `dex()` wrapper (GNU `timeout`,
`DEX_TIMEOUT` default 30s, env-overridable, bare-docker fallback when the
`timeout` binary is missing); the `docker ps` listing fails closed on a
wedged daemon; per-instance `[i/N]` progress lines show the operator exactly
where a hang was. Message texts are otherwise unchanged.

## What rides apply-all

No new stage files: all four scripts are already in the apply-all stage
list, and this rides it from repo HEAD. Suite `t104` in preflight:
`pending-deploy/push-timeout/test_push_timeout.sh` (66/66: fake docker with
hang modes, real timeout integration, no-timeout fallback, source
contracts).

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH (scripts are staged to /root/jerusalem/):
for f in push-keepalive.sh push-booted-watcher.sh push-screencap.sh push-optimizer.sh; do
  scp ~/workspace/droidfarm/pending-deploy/*/$f root@64.177.49.68:/root/jerusalem/$f
done
# no restart needed — finish-deploy.sh runs them from disk per invocation
```

## Verify

- `bash -n` clean on all four scripts.
- On the VPS: `DEX_TIMEOUT=1 bash /root/jerusalem/push-keepalive.sh` against
  a healthy fleet still prints `[i/N]` progress lines and `pushed to N
  instance(s)` with exit 0 (1s is plenty for healthy `docker exec` calls).
- `finish-deploy.sh` steps 1–4 show per-instance `[i/N]` lines in order.
- Simulate a wedged daemon (`systemctl stop docker` in a test window —
  operator-visible, recover with `systemctl start docker`): the push step
  prints `FAIL: docker ps failed (rc=...) - docker daemon may be wedged`
  and exits 1 within ~30s instead of hanging.
