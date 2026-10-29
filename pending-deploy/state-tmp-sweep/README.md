# state-tmp-sweep

Follow-up to the staging-file audit thread (login-pwd-cleanup ->
login-script-cleanup -> waydroid-exec-cleanup -> remote-b64-sweep):
those sessions closed every staging file in the instance volumes, but
the manager's own state dir used the same atomic-write idiom with no
sweep anywhere. A crash mid-`save_*` left `FILE.tmp` behind forever,
including plaintext credential dumps (`roblox_accounts.json.tmp`,
`tokens.json.tmp`, `static_join_token.tmp`).

This package adds the missing startup sweep for the state dir +
batch-journal tmps, and fixes the two permission holes found in the
same pass (`save_tokens` never 600, `save_accounts` 600 only after
the replace).

Rides apply-all from HEAD (t126). See DEPLOY.md for deploy + verify.
