# state-migrate

One-command import of the fleet's portable state into the NEW VPS
(64.177.49.68): `import-state.sh <backup-state.sh archive> [--full]`.

See DEPLOY.md for the full SSH-session recipe (export on the old box,
copy, import on the new box).

## Design notes

- The import set is portable data only: Roblox account mapping, login
  results, fleet-job journals. Topology (`instances.json`) and identity
  (`tokens.json`, `static_join_token`) stay on the destination — the new
  box creates its own fleet and keeps its own join token.
- `--full` exists for a full-box swap only; it is documented as wrong
  when the new box will create its own fleet (entries point at the old
  host's containers).
- Fail-closed on hostile archives: member whitelist against the
  canonical state set + hex-only journal names; absolute paths, `..`,
  and unexpected members abort before anything is written.
- Snapshot-before-write: every import first copies the destination's
  current state to a timestamped 700-perm dir, so a bad import is
  reversible by copying files back.
- Journals merge (additive), never replace — the destination's own
  journals survive.
- All helpers best-effort where safe, never prints file contents;
  `systemctl` and the dashboard probe are warn-only so a stopped service
  can never brick the import.

## Tests

`test_state_migrate.sh` — 59 hermetic checks against the real script
(fake root via env overrides + fake id/systemctl): usage/non-root/
missing-archive/non-tar failures, 5 hostile-archive rejections (each
leaving the dest untouched), corrupt-JSON fail-closed, happy path
(portable data imported, topology + identity preserved, perms
600/700, snapshot holds pre-import state, no secret in output),
`--full` semantics, missing-member warnings, svc stop+start wiring,
stop-failure warn-and-continue, idempotent re-run, `./`-prefix and
dir-entry tolerance, bash -n clean.
