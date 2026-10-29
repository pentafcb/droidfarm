# non-dict-json-guard (t150)

Non-dict JSON bodies no longer drop the request handler.

- `manager/app.py` `_read_json`: non-dict parsed values coerce to `{}`.
- `wing/agent.py` `_read_json`: same coercion; VERSION 1.0.44 -> 1.0.45.
- `manager/app.py` roblox-account route: non-dict body -> clean 400.
- Tests: `test_non_dict_json_guard.py`, 39/39 — versions + snapshot
  parity, regression proof, manager + agent `_read_json` matrices,
  real `do_POST` roblox-account behavior, source contracts.
- Preflight: t150 in `pending-deploy/apply-all/deploy-all.sh`.
- Deploy: rides apply-all from HEAD; see DEPLOY.md.
