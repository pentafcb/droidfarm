# missing-frame-guard

## The gap

`live.png` is the only frame source the streams read. Both
stale-stream repairs assumed the file EXISTS and is old:

- Main-node watchdog (`df-watchdog.sh` section 4): the whole block
  sat inside `if [ -f "$png" ]`. A loop that never produced a frame
  (screencap failing every cycle: no display, broken surface) never
  tripped it.
- Wing agent (`wing/agent.py` selfwatch check 3): `os.path.getmtime`
  raised on the missing file, `age` became `-1`, and `age >= 30`
  was false. Same hole on remote nodes.

User-visible symptom: the viewer reconnected MJPEG forever (the
socket upgrades fine; the server loop just never delivers a frame
and the 15s frameless guard closes it, then the dashboard retries),
with no watchdog repair and no "needs attention" badge — because
every repair path keyed on staleness, and a missing file is not
stale.

## The fix

Treat "missing" as a third stream state alongside "fresh" and
"stale", with the same remedy and the same budget:

| state | condition | action |
|---|---|---|
| fresh | file exists, age <= 30s | none |
| stale | file exists, age > 30s | pkill + restart loop (staleloop budget) |
| missing | file absent, booted, loop alive, script present | pkill + restart loop (SAME staleloop budget) |

Guards on the missing branch (both sides):

- **Booted gate.** A first-boot instance legitimately has no frame
  yet; the branch requires the `booted` marker (watchdog:
  `$base/booted`; agent: `sw_booted_once()`).
- **Loop-alive gate.** A dead loop is the helper check's job. The
  missing branch pgreps the loop first, and skips when a loop
  restart was already issued by the helper check this pass/cycle
  (watchdog `_loop_restarted` flag set in section 3; agent
  `loop_restarted` flag set in check 2) — so one pass never
  restarts the loop twice.
- **Unknown gate.** A timed-out pgrep (exit 124) is UNKNOWN, not
  dead: skip, never burn budget.
- **Missing-script gate.** Script not in the data volume -> the
  shared per-day WARN, no restart, no budget burn (same as stale).
- **Shared budget.** Stale and missing share one circuit-breaker
  key (`$i.staleloop` / `name + ".staleloop"`, 3600s/10), so a
  flapping loop can't burn both. Trip -> DEGRADED marker, which
  the existing dashboard badge already surfaces.

Why firing immediately is safe: the loop installs `live.png` by
atomic `mv`, so a missing file is never a mid-write transient — it
means no capture ever succeeded (or the file was deleted). A
healthy loop re-creates it within one cycle.

## Why both sides

The watchdog runs main-node only; the wing agent's selfwatch is the
remote-node equivalent. The hole existed in both, so the fix lands
in both with identical semantics and the same version-bump rule
(agent 1.0.41 -> 1.0.42 so nodes self-update to it).

## Tests

`test_missing_frame_guard.py`, 27 checks, hermetic:

- A. versions (1.0.42, snapshot parity, manager gate, historical
  1.0.41 focus-gate comment untouched).
- B. agent: missing fires / not-booted skips / dead-loop single
  restart (flag) / helper-budget-out pgrep gate / missing-script
  WARN / DEGRADED blocks / stale + fresh regressions / 11-pass
  budget trip at exactly 10 restarts.
- C. watchdog (real script vs fake docker): same matrix, plus
  pgrep-timeout skip and the 11-run budget trip.
- D. source contracts: shared staleloop keys (2 call sites each
  side), flag wiring, booted gates, no destructive commands in
  the new watchdog block.

The old `remote-selfwatch` suite's missing-png assertion ("no
action") was updated to the new contract: missing + booted + loop
alive now restarts with the "screencap missing" log.
