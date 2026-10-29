# numeric-overflow-guard

Same defect class as t148 (input-coord-validation), everywhere else it
lived: `json.loads` accepts `Infinity`/`NaN` and huge ints, and
`int(Infinity)` / `float(10**400)` raise `OverflowError` — a class the
numeric-conversion except clauses didn't catch (only `TypeError` /
`ValueError`). The request thread died with no response instead of a
clean 400 on create, fleet-plan, fleet-create, resize, and both wing
agent routes; the "never raises" resource/drift/heartbeat sanitizers
could break `/api/instances` on a bad agent payload.

Fix: `OverflowError` joins the except tuples at 9 manager sites and
both agent routes (agent 1.0.43 -> 1.0.44). Valid inputs byte-identical.

Tests: `test_numeric_overflow_guard.py` — 43/43 (versions + snapshot
parity, regression proof, validator matrix, sanitizer matrix, agent +
manager source contracts). Preflight t149. See DEPLOY.md for the deploy
and verify steps.
