# remote-input-b64 — design notes

## The bug
`manager/app.py::_roblox_login_remote` sends the Roblox password to the
wing agent as `{"type": "text", "text": "<base64>", "b64": true}`. The
agent decoded it and ran it through `sanitize_input_text`, which strips
everything outside `[a-zA-Z0-9%s.,!?@_-]`.

Consequences:
- Any remote-node account whose password contains `#$&*()+=/:;<>[]{}|\'"`~^`
  (or several others) could NEVER log in via the dashboard Login button,
  the fleet "Login all", or auto-login — the typed password was wrong.
- The failure surfaced as a generic login failure (recorded by
  login-observability), indistinguishable from a wrong password.
- The same password worked on local instances, where the manager writes
  `pwd.txt` and runs `input text "$P"` — a local/remote parity gap.

The `b64` flag's own comment said it was for "shell-special characters",
but the sanitize made the flag pointless.

## Why a file, not inline quoting
The container's input daemon does `read cmd < $FIFO` then `eval "$cmd"`,
so the FIFO line is parsed by sh. Any password bytes interpolated into
that line would need shell-quoting — fragile against quotes, `$`,
backticks, newlines. The file indirection (`input text "$(cat <path>)"`)
keeps zero secret bytes in the command line; POSIX guarantees expansion
results inside double quotes are not re-parsed, so every byte is inert
data. This mirrors the manager's local `pwd.txt` approach exactly,
including `input text`'s `%s`→space decoding (parity, not a new quirk).

Alternatives considered:
- Inline `input text "$(echo <b64> | base64 -d)"`: no file, but depends
  on `base64` existing in the container's toybox — not verified in this
  Waydroid build. `cat` is verified (the local login path uses it).
- Keep sanitize for b64 too: preserves the bug. Rejected.

## The delayed delete
The staged file holds a secret, so it must not linger: a daemon thread
deletes it after `_B64_TEXT_TTL_S` (60s; test seam). Deletion is
content-matched — if a newer stage overwrote the file, the older
deleter leaves it alone. A 60s window is generous: a successful FIFO
write means a reader is consuming (O_NONBLOCK open fails with ENXIO
when no reader exists, so `input_linux` 500s first).

## The manager gate bump (1.0.2 → 1.0.38)
Agents < 1.0.38 mangle; they cannot be fixed from the manager side. The
gate now refuses them loudly ("node agent too old for auto-login (needs
1.0.38+)") instead of failing logins silently. The wing-update-path fix
(2026-10-23) unblocked the self-update mechanism, so the node picks up
1.0.38 on its next heartbeat after the manager deploys — the refusal
window is minutes, and manual Login/Login-all can retry after.

## Known limitation (both paths, pre-existing)
Android's `input text` decodes `%s` → space, so a password containing the
literal two-character sequence `%s` is typed with a space on BOTH local
and remote paths. Fixing it needs per-character keyevent typing
(char→keycode map with shift handling) — disproportionate risk for a
rare edge; documented here instead.
