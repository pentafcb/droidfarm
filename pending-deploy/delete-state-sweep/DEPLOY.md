# DEPLOY: delete-state-sweep

## What

Deleting an instance left stale per-instance state behind for a
recreated instance to inherit (see README.md).

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py` and `wing/agent.py`; no new stage files.

## Standalone (if apply-all is unavailable)

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp wing/agent.py root@209.222.10.93:/root/jerusalem/agent.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

The wing agent is picked up by nodes via the manager's agent
self-update path (sha-driven); a node installer re-run also works.
No restart of any instance is needed — the sweep runs on the next
delete, and already-stale markers for deleted names are harmless
(they are only read for names present in instances.json).

## Verify

1. `systemctl is-active jerusalem` -> active.
2. Dashboard root returns HTTP 200.
3. Create a throwaway instance, wait for it to boot, then delete it
   and recreate the same name: the new card shows no login badge,
   no "needs attention" badge, and no version-drift badge from the
   deleted instance (only its own live state).
4. On the node side (after the agent updates): same delete +
   recreate cycle on a wing node leaves no stale selfwatch marker in
   the Nodes modal for the new instance.
