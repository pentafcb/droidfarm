# http-server-bounds

Slowloris / thread-starvation hardening for both HTTP servers.

## Problem
Stock `ThreadingHTTPServer` = one thread per connection, no cap, no
per-connection timeout. Failure modes on an internet-exposed port:
- A client that connects and sends nothing holds a handler thread **forever**.
- A client that dribbles one byte per minute holds a thread **forever**.
- Enough of them (or a wedged proxy holding connections) starves the whole
  server: new requests queue behind thread creation until the process wedges
  — the fast-200-then-mid-body-drop signature seen 2026-10-11.

## Fix
`BoundedHTTPServer(ThreadingHTTPServer)` in both `manager/app.py` and
`wing/agent.py` (duplicated — the agent is a single deployed file):
- `max_threads = 64`: a `BoundedSemaphore` caps concurrent handler threads.
  When saturated, the newcomer gets `HTTP/1.1 503` immediately instead of
  queueing another thread. Legit peak on this box is < 20.
- `conn_timeout = 60`: `get_request()` sets `SO_TIMEOUT` on every accepted
  socket. Idle keep-alive connections, half-opens, and dribble-feeds are
  dropped after 60s of inactivity, freeing the slot. Active streams are
  unaffected (frames flow constantly).
- `request_queue_size = 128`: deeper listen backlog so bursts queue in the
  kernel instead of being refused while a slot frees.
- `_process_request_guarded`: same as `ThreadingMixIn.process_request_thread`
  plus guaranteed slot release in `finally`.

Both are class attributes so tests subclass with tiny bounds.

## Files
- `test_http_server_bounds.py` — 38 hermetic checks (localhost sockets only).
- `DEPLOY.md` — deploy + verify steps.
- Product changes: `manager/app.py`, `wing/agent.py` (VERSION 1.0.8).
