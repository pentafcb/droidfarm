# Deploy: comma-tolerant bulk account import

## The bug

`parse_bulk_accounts()` (manager/app.py, the fleet-bar Accounts bulk
import) split each line on commas and took the third field as the
password verbatim. A password containing a comma was **silently
truncated at the first comma** — the remainder vanished, the saved
credential was wrong, and every later login for that instance failed
with no visible link back to the import. ("commas inside password are
not supported" was even documented as intended behavior in the old
suite.)

## The fix

- The name is the text before the first comma, the username the text
  between the first and second comma, and the password is **everything
  after the second comma**. Internal spaces are preserved.
- Only a *trailing* comma-separated segment that is exactly an auto
  literal (`1/true/yes/auto/y`, case-insensitive) is stripped as the
  auto-login flag.
- Ambiguity (documented, not silent): a password that literally ends
  in `,true` (etc.) cannot be imported this way — set it in the
  per-instance Account panel instead.
- Truncation (username 100 / password 200), per-line error reporting,
  unknown-instance refusal, merge semantics, and the bulk-accounts
  summary secrecy are unchanged.
- Dashboard Accounts modal gains one Spanish hint line:
  "La contraseña puede contener comas; un cuarto campo
  (auto/true/1/yes/y) activa el inicio automático."

## What rides apply-all

No new stage files: `manager/app.py` + `manager/static/index.html`
are already in the apply-all stage list, and this rides it from repo
HEAD. Suite `t99` in preflight:
`pending-deploy/bulk-accounts-commas/test_bulk_accounts_commas.py`
(24/24: the bug regression, flag handling, edge cases, an end-to-end
save through `bulk_save_accounts`, source + dashboard contracts).
The old bulk-accounts suite's stale contract assertion was updated to
the new behavior (still 59/59, count-neutral).

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH:
scp ~/workspace/droidfarm/manager/app.py \
    root@<box>:/root/jerusalem/app.py
scp ~/workspace/droidfarm/manager/static/index.html \
    root@<box>:/root/jerusalem/static/index.html   # served from disk, no restart
systemctl restart jerusalem   # needed for app.py (manager code)
```

## Verify

- `systemctl is-active jerusalem` → active; dashboard loads (HTTP 200).
- Accounts → import `rbx1,user1,pa,ss,word` → import once → the
  per-instance result lists it as saved; the viewer Account panel
  shows a saved password for rbx1, and a Login attempt uses the full
  `pa,ss,word` (watch the login-result badge, not a silent failure).
- Import `rbx2,user2,hunter2,auto` → auto-login flag still lands.
