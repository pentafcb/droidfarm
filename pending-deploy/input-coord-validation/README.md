# input-coord-validation

Viewer input coordinates are normalized 0..1000 by the dashboard, but
neither server validated them — and Python's `json.loads` accepts
`Infinity`/`NaN`. A crafted tap with x=Infinity reached `int(float(x))`,
which raises `OverflowError`, a class neither except clause caught
(both caught only `TypeError`/`ValueError`). The request thread died
and the connection dropped with no response instead of a clean 400.

Fix, same contract on both sides:

- `manager/app.py`: `do_instance_input`'s `scale()` now float-converts,
  then rejects non-finite and out-of-range coords (`0 <= fx <= 1000`
  per axis; NaN fails every comparison, inf fails the upper bound).
  Tap and swipe share `scale()`, so both are covered; valid inputs
  produce byte-identical commands.
- `wing/agent.py` (1.0.42 -> 1.0.43): new `_agent_tap_cmd(data)` helper
  with the same validation (same shape as `_agent_text_cmd`); the
  do_POST tap branch calls it, so a bad tap is a clean 400.

Tests: `test_input_coord_validation.py` — 38/38 hermetic (real modules,
stubbed container/exec): version + snapshot parity, valid taps/swipe/
key incl. boundaries and truncation, invalid matrix -> clean 400 with
exec never called, regression proof that `int(inf)` raises
`OverflowError` (not a `ValueError`, so the old handler necessarily
missed it), and source contracts (range check, helper wiring, no
unguarded `int(float(` left in either input path, dashboard clamp
unchanged).

Rides apply-all from HEAD (preflight t148); no new stage files.
