# static-path-guard (t158) — DEPLOY

## What it is

Repo-side preflight gate + one manager fix that rides apply-all from
HEAD (`manager/app.py` already in the stage list — no new stage files,
no agent bump, no dashboard change, no service restart beyond the
apply-all restart itself).

## The fix

`manager/app.py` `_serve_static` (reachable from an unauthenticated GET
— `/` and `/static/*` are public by design, the dashboard page itself)
checked containment with a bare prefix test,
`fpath.startswith(STATIC_DIR)`. After `os.path.normpath` resolves
`..`, a request for `/static/../static-evil/secret.txt` resolves to
`<dir>/static-evil/secret.txt`, which starts with `<dir>/static` and
was served with HTTP 200. Only a sibling DIRECTORY sharing the
`static` prefix was exposed: a sibling file (`../app.py`,
`../instances.json`) never matched the prefix, and URL-encoded
traversal (`%2e%2e`) is never decoded by `do_GET`'s `urlparse` path,
so it stayed a literal directory name. Containment is now a
whole-directory match: `fpath == STATIC_DIR or
fpath.startswith(STATIC_DIR + os.sep)`. Legitimate in-tree traversal
(`/static/sub/../index.html`) still resolves inside the tree and
still serves.

## What the suite checks

`test_static_path_guard.py` (20 checks, hermetic, no network):
regression proof (the old expression allows the sibling escape, the
new one blocks it, a sibling file never matched either); real
`_serve_static` on a fake tree (stubbed `_send`/`_json`, patched
`STATIC_DIR`): `/` and `/static/*` serve, nested files serve, legit
in-tree dotdot serves, both `static-evil` and `static-backup`
sibling escapes 404, above-tree escape 404, sibling file 404, missing
404, encoded traversal 404, absolute segment 404; source contracts
(fixed expression present, bare expression gone, `isfile` kept,
static routes still served only via `_serve_static`); snapshot parity
(remote-autologin `app.py` == `manager/app.py`).

## Deploy

No standalone step: run `pending-deploy/apply-all/deploy-all.sh`
from an SSH-capable session (or `sync-newbox.sh --yes` for the new
box). Verify after deploy: `curl` the live dashboard root still
returns 200, and `curl http://<vps>:8080/static/../static-evil/x`
returns 404 (there is no such sibling directory on the VPS today, so
the live check proves the route shape, not the fix — the fix itself
is proven by the hermetic suite above).

## Blocked on

VPS SSH in worker sessions (same as the rest of the queue).
