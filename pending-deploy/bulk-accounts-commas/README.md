# bulk-accounts-commas

Silent credential corruption in the fleet-bar bulk account import:
`parse_bulk_accounts()` truncated any password at its first comma
(third field verbatim), so the saved password was wrong and every
later login for that instance failed — with no visible link back to
the import.

## Contract (new)

`name,username,password[,auto]` where:

- `name` = text before the first comma (the anchor),
- `username` = text between the first and second comma,
- `password` = everything after the second comma — commas and
  internal spaces preserved,
- a *trailing* segment that is exactly an auto literal
  (`1/true/yes/auto/y`, case-insensitive) is stripped as the
  auto-login flag.

A password that literally ends in `,true` (etc.) is ambiguous and
cannot be imported this way — the Account panel takes it verbatim
instead. Everything else (truncation, per-line errors,
unknown-instance refusal, merge, summary secrecy) is unchanged.

## Files

- `test_bulk_accounts_commas.py` — 24 hermetic checks (bug regression,
  flag handling, edge cases, end-to-end save, source + dashboard
  contracts). Preflight `t99`.
- `DEPLOY.md` — standalone scp + verify steps.
- The product change lives in `manager/app.py`
  (`parse_bulk_accounts`) + `manager/static/index.html` (one Spanish
  hint line); both already in the apply-all stage list.
