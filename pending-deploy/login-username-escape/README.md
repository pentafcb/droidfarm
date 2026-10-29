# login-username-escape

## Gap

`_roblox_login_inner()` (manager/app.py, local-instance branch) built the
login shell script with an ad-hoc username escape:

```python
safe_user = username.replace(" ", "%s").replace("'", "").replace('"', "")
...
f"input text {safe_user}",
```

`auto_login.sh` runs under `sh` as root inside the Android container, so
every shell metacharacter the denylist missed — `$(...)`, backticks, `;`,
`|`, `&`, newlines, `(`, `)`, `<`, `>`, `#`, `~`, `\`, `!` — executed as
container root whenever a login sequence ran. Saved usernames enter
verbatim via the dashboard Account panel and the bulk account import (only
length-truncated to 100 chars). The `/input` route's text allowlist
(`[^a-zA-Z0-9%s.,!?@_-]`) and the wing agent's 1.0.2 `sanitize_input_text`
already covered their sinks; this local login script was the one remaining
unsanitized producer (and the in-container `input-daemon.sh` runs
`eval "$cmd"` on FIFO lines, so quoting must be airtight). Authenticated
scope only (dashboard login required), container scope (not the host) —
defense in depth, same class as the route-name-gate work.

## Fix

```python
safe_user = shlex.quote(username.replace(" ", "%s"))
```

plus `import shlex` at module level. The `%s` space encoding `input text`
needs is preserved; `shlex.quote` makes everything else inert under POSIX
`sh` (the container's `sh` is dash — the quoting is POSIX-compatible).
Legitimate usernames type byte-identically, including emails with
`@`/`.`/`+` (all in shlex's safe set, no quotes added) and names with
spaces (`cool user` -> `input text cool%suser`, unchanged from before).
The password line `P=$(cat ...); input text "$P"` is untouched — the
double-quoted `$P` was already safe (substitution results are not
re-scanned).

## Tests

`test_login_username_escape.py`, 51/51 hermetic checks (real app import,
faked accounts/instances/waydroid_exec/sleep): 14 hostile usernames are
driven through the real `_roblox_login_inner`, and the generated script is
executed by a REAL `sh` with stubbed `input`/`touch`/`id`/`reboot` — every
payload must execute nothing and arrive byte-exact as typed text. A
vulnerability proof runs the old escape's output for the same payload and
confirms it DOES execute (proving the test catches the bug). Legit names
assert exact script lines plus transparent typing; the password line is
asserted byte-identical; source contracts assert the ad-hoc escape is gone
and `shlex.quote` is at the sink; edge cases (`""`, `None`, unicode) never
raise.

## Wiring

Preflight t91 (`"PASS=51 FAIL=0"`). No new staged files — `manager/app.py`
already rides apply-all from HEAD.
