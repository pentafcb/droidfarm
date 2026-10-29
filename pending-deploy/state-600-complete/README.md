# state-600-complete

Closes the state-file 600 audit: `manager/app.py` `save_instances`
(no chmod at all) and `save_login_results` (chmod after the
replace — the last instance of that flaw in the manager), plus
`wing/agent.py` `save_instances` (no chmod on the node side), all
now chmod their tmp to 600 BEFORE the replace. After this, every
`save_*` in the manager and the agent's `save_instances` is
chmod-before-replace. The next save after deploy repairs
already-deployed files automatically; no agent version bump
(self-update is sha-driven, VERSION stays 1.0.38).

Tests: `python3 test_state_600_complete.py` — 33/33 hermetic (real
app + real agent import, tmp state dirs, umask 022). Wired as
preflight t128. Rides apply-all from HEAD; see DEPLOY.md.
