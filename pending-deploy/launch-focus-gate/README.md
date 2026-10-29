# launch-focus-gate

## The bug

`openViewer()` fires POST `/api/instances/<name>/launch-roblox` on
EVERY viewer open, and the fleet "Roblox all" job fires it on every
instance — and both launch paths tapped blindly: HOME, app-drawer
swipe, icon tap at 129,424. On a healthy instance the in-container
keepalive already keeps Roblox focused, so opening the viewer to
WATCH a running game yanked the user out of it: HOME exits the game,
the drawer tap relaunches Roblox cold, and the session the user
wanted to watch was gone. The tap was redundant as well as
destructive — and the keepalive then spent its next cycles
relaunching what the viewer-open had just killed.

## The fix

Both launch paths probe focus first (`dumpsys window`'s
`mCurrentFocus`, the same probe the keepalive uses) and only tap
when the probe answers "not focused":

- `manager/app.py`: new `roblox_focused(name)` (True / False /
  None) + gate in `launch_roblox()`.
- `wing/agent.py` 1.0.40 → 1.0.41: new `roblox_focused_linux(name)`
  (same tri-state, list-form `docker exec`) + gate in
  `launch_roblox_linux()`. Nodes self-update to 1.0.41 on the next
  heartbeat after the manager deploy.
- Focused → success, zero taps (the viewer status "Roblox abierto"
  is then literally true). Not focused → the unchanged tap
  sequence. Probe unknown (error, timeout, invalid name) → failure,
  zero taps.

The unknown case deliberately diverges from the keepalive, which
taps on probe error: the keepalive retries every 10s as recovery,
while a dashboard launch is a single user action where a false
failure is safe (retry from the viewer Roblox button or "Roblox
all") and a false tap destroys a session.

## What it is not

- Not a dashboard change: viewer-open still auto-launches — now
  safely, as a no-op when Roblox is already focused.
- Not a login change: the login tap sequence (`roblox_login`) must
  tap even when Roblox is focused (it fills fields); it is untouched.
- Not a keepalive change: the in-container loop's own focus probe
  and budget are untouched.
- Not a tap-coordinate change: the 129,424 sequence is byte-identical.

## Tests

`python3 test_launch_focus_gate.py` → 35/35 (focus tri-state on
both sides incl. first-`mCurrentFocus`-wins and the keepalive
no-match fallthrough, launch gating incl. zero-tap proofs,
unknown-probe failure proofs, invalid-name proofs, exact tap
sequences, agent 1.0.41 bump + list-form docker call, dashboard and
route contracts).
