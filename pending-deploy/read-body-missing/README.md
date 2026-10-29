# read-body-missing: Handler._read_body never existed

## The bug

POST `/api/instances/<name>/resize` and POST `/api/instances/<name>/roblox-account`
both called `self._read_body()`. No such method was ever defined on `Handler`
(or inherited from `BaseHTTPRequestHandler`), so both routes raised
`AttributeError` and the connection dropped with no response at all:

- Dashboard **Resize** (per-instance RAM/CPU) never worked.
- Dashboard **Account Save** in the instance viewer (username/password/
  auto-login per instance) never worked.

Bulk account import uses `/api/roblox-accounts/bulk`, a different endpoint,
which is why the breakage went unnoticed. The roblox-account call site dates
to the first DroidFarm source commit; the resize call site joined it with
instance-resize (663c8ab), which tested `validate_resize_params` and the
resize cores but never the route handler — so the 77/77 suite passed while
the route itself was dead.

## The fix (manager/app.py only)

New `Handler._read_body(max_bytes=2097152)`, placed next to `_read_json`,
with the same 2MB cap semantics: over-cap or garbage `Content-Length`
returns `b""` **without reading** the body (a declared 10GB body must never
be paged into RAM), and it never raises. The two call sites are unchanged,
so they still do `json.loads(body or "{}")` — invalid JSON still surfaces
as their existing 400, and the auth gate in front of both routes is untouched.

A sweep of every `self._*` call in `Handler` (and in the wing agent's
`Handler`) confirms `_read_body` was the only missing method; the agent is
clean.

## Tests

`test_read_body_missing.py`, 20/20: method existence + both call sites,
unit behavior (exact bytes, empty/missing length, over-cap without reading,
negative/garbage length, at-cap reads), pre-fix regression proof against
the original commit (method absent, call site present), and a live
`BoundedHTTPServer` section — login, account save 200 + persisted, invalid
JSON 400, resize-empty-params 400, unauthenticated 401, clear 200.
The live section uses a temp `JERUSALEM_DATA` and a test-only password,
so no real state is touched.
