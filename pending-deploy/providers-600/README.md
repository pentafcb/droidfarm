# providers-600

Closes the last credential-permission hole in the manager's atomic
saves: `accounts` and `tokens` were hardened to 600 by state-tmp-sweep,
but `providers.json` — which holds every wing node's `agent_key` —
was never chmodded and sat world-readable under umask 022. The fix is
one chmod-before-replace in `save_providers` (manager/app.py only).
The dashboard API already strips the key from its response, so disk
was the only leak path; the next heartbeat save after deploy repairs
already-deployed files automatically.

Tests: `python3 test_providers_600.py` — 18/18 hermetic (real app
import, tmp state dir, umask 022). Wired as preflight t127.
Rides apply-all from HEAD; see DEPLOY.md.
