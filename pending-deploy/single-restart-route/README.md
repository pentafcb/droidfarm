# single-restart-route — per-instance Restart: route + card button

## The gap

`instance_single_action()` has supported `"restart"` (stop-then-start,
local + remote) since batch-ops, and the batch worker uses it for
"Reiniciar todas" — but the per-instance HTTP route only matched
`(start|stop)`, so `POST /api/instances/<name>/restart` fell through to
the do_POST 404. The dashboard never sent it (cards had no Restart
button), so the hole was latent, not user-visible — the same class as
read-body-missing: a route the core supports but the handler never
exposes. Restarting ONE instance meant Stop then Start by hand, or
restarting the whole fleet.

## Changes

**`manager/app.py`** (rides apply-all from HEAD — no new stage files):
- The single-action route regex widens to `(start|stop|restart)`. The
  handler body is unchanged: it already passes `m.group(2)` straight to
  `instance_single_action()`, which handles restart for local
  (docker stop, pool prepare, docker start) and remote (wing stop then
  wing start) instances. Invalid names still 404, unknown actions still
  fall through to the 404, the auth gate still runs first (all proven
  by the suite's real-dispatch tests, not regex literals).

**`manager/static/index.html`** (already staged):
- Card row gains a "Reiniciar" button (`data-act="restart"`), next to
  Iniciar/Detener. `act()` already posts generic actions to
  `/api/instances/<name>/<action>`, so no new send path was needed;
  restart asks for confirmation first, like Eliminar and the fleet
  stop/restart buttons.

## Deliberate boundaries

- No new server behavior: restart semantics are exactly the batch
  worker's (stop-then-start, one instance's failure never affects
  others — here there is only one instance).
- Restarting a stopped instance behaves like fleet "Reiniciar todas"
  (stop is a no-op on a stopped container, then start) — no new guard
  was added, so the single and fleet paths stay identical.
- The batch-ops suite's "batch not swallowed" assertion still passes:
  the widened regex cannot match `/api/instances/batch` (one segment,
  and "batch" is not an action).

## Tests

`test_single_restart_route.py` — 30/30 (hermetic): live regex extracted
from do_POST source (restart/start/stop match, unknown action and
batch/fleet paths don't), route placement (batch/fleet exact routes
precede the action route), real dispatch through `Handler.do_POST`
(restart/start/stop reach the core, unknown action/invalid name/401
never do), core restart meaning (local stop-then-start order, remote
wing stop-then-start), dashboard contract (button, label, confirm,
sibling buttons intact, Spanish).
