# wing-installer-staging

Puts the wing node installers under the same deploy discipline as the
rest of the queue.

The manager's `/api/install/` route serves three files from
`WING_DIR` on disk: `agent.py`, `install-wing.sh`, `install-wing.bat`.
Until now only `agent.py` rode `apply-all`; the two installers were
invisible to the deploy pipeline (and to the live-verifier), so
installer fixes — e.g. the marker-verified `agent.py` download in
`install-wing.sh` — could only reach the VPS by hand-rolled scp.

This package adds `wing/install-wing.sh` and `wing/install-wing.bat`
to the 19-file stage (`SOURCES`/`STAGE_NAMES`/`KEYS`), maps them to
`/root/jerusalem/wing/` on the VPS, gates the `.sh` with `bash -n`
on both sides of the deploy, and adds the `--ssh` currency probes.
The 32-check hermetic suite also enforces the token-hygiene rule:
every `JOIN_TOKEN=` assignment must be exactly the
`__JOIN_TOKEN__` placeholder (no hardcoded real token, the
2026-10-06 bug class), and placeholders are never baked in — the
manager substitutes them at serve time.
