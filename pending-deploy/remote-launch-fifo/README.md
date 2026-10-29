# remote-launch-fifo

## Problem
The wing agent's `launch_roblox_linux()` wrote the 5-command launch
sequence (HOME, drawer swipe, Roblox tap) as FIVE separate `os.write()`
calls on one FIFO fd, while the input daemon does `read cmd < $FIFO`
(one line per FIFO open, reopening after every line). Once the writer
closed, the lines after the first were discarded — remote "Launch
Roblox" (dashboard button, fleet "Roblox all", batch `launch-roblox`
on a wing node) only ever executed HOME and never opened the drawer
or tapped the icon. Depending on timing the writer could also hit
EPIPE mid-loop (daemon closed its read end after line 1 while the
writer was still writing), surfacing as a 500 instead of a launch.

The manager's LOCAL path was fixed to one compound write in October
(the FIFO multi-command delivery fix); the agent's REMOTE path was
never fixed — a local/remote parity gap, same class as
remote-input-b64 (remote login passwords mangled by sanitize).

## Fix (wing/agent.py only)
- `launch_roblox_linux()` sends ONE compound write (`"; ".join(cmds)`)
  per FIFO open, mirroring the manager's local `_send_input_cmds`.
- The sequence now matches the manager's verified local sequence
  (swipe settle sleep 2, not sleep 1). A parity check in the suite
  parses both sequences and fails if they ever drift again.
- Name gate, O_NONBLOCK open, and OSError → False behavior unchanged.

## Why no version bump
No manager gate depends on launch (only login gates on agent
>= 1.0.38, still satisfied), and self-update is sha256-driven, so the
fix ships on the next heartbeat after the manager deploy — same
precedent as agent-ip-gate. A bump would cascade through ~35
VERSION-membership suites for zero gate benefit.

## Tests
`test_remote_launch_fifo.py` — 19 checks: daemon-faithful delivery
(real FIFO + reader thread modelling `read cmd < $FIFO` per
iteration, eval delay between lines) proves the old 5-write delivery
loses everything after HOME while the new compound write delivers the
full sequence as one line; name gates; missing-FIFO and no-daemon
failures return False without hanging; source contracts (compound
join present, per-line loop gone, fits PIPE_BUF); manager-sequence
parity; VERSION still 1.0.38.

## Wiring
Preflight t121, exact-count `PASS=19 FAIL=0`. No new stage files —
`wing/agent.py` rides apply-all from HEAD. The remote-autologin
staged `agent.py` snapshot is refreshed to HEAD (queue-audit check 4).
