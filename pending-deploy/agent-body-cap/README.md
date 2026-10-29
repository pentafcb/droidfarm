# agent-body-cap

Wing agent 1.0.37 closes the last unbounded read on the fleet's HTTP
servers: `Handler._read_json` now caps request bodies at 2MB, mirroring
the manager's `_read_json` contract (shipped with the login hardening).

Why: the agent did `self.rfile.read(n)` with `n` straight from the
client's Content-Length. An authed client declaring 10GB would page the
whole body into RAM before `json.loads` ran. The 64-thread /
60s-socket-timeout bounds (http-server-bounds, 2026-10-11) bound time and
threads, not bytes — 64 handlers x 60s of inbound bytes is a
memory-exhaustion DoS on every enrolled node (vultr + Penta's PCs).

Fail-closed shape: over-cap returns `{}` without touching `rfile`
(HTTP/1.0 closes the connection after the response, so skipping the read
cannot desync anything); negative/garbage lengths and malformed bodies
return `{}`; never raises. The three `do_POST` call sites keep the default
cap, so this is invisible to legitimate traffic — routes just see an empty
body and answer their normal 400s.
