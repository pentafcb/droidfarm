# dashboard-html-escape (t159) — DEPLOY

## What it is

Repo-side preflight gate + one dashboard fix that rides apply-all from
HEAD (`manager/static/index.html` already in the stage list — no new
stage files, no agent bump, no manager change, no service restart
beyond the apply-all restart itself; the file is served from disk, so
the fix is live as soon as it lands).

## The gap

The dashboard renders server data through `innerHTML`, and most sinks
already escaped their values (badges, provider names, bulk-import
results, install logs all go through `esc()`) — but the instance CARD
did not: the card template interpolated `i.name`, `i.ip`, `i.status`
(twice: class attribute + text), `i.cpus` and `i.memory_mb` RAW, plus
`i.memory_mb` raw in the usage line, the node-failure count raw in the
`nodeFetchBadge` title, `p.total` raw in the fleet-create button label,
and `p.id` raw in the Nodes-modal `data-info` / `data-del`
attributes. Today those values are server-shaped (instance names are
create-time-validated, provider ids are server-generated `p_hex`), so
nothing exploitable was live — but the card was the one place a
corrupted record, a future field, or a hostile node agent could have
turned into stored HTML injection in an authenticated dashboard
session, and the class-attribute interpolation (`dot ${i.status}`) is
exactly the shape that breaks out of an attribute.

## The fix

Every one of those interpolations now goes through `esc()`, which
escapes `& < > "` — enough for both the text nodes and the
double-quoted attributes used here (`data-info` / `data-del` are
double-quoted; `esc` covers the quote). Valid data renders
byte-identically; only markup inside a value changes (it now shows
literally instead of parsing).

## What the suite checks

`test_dashboard_html_escape.py` (12 checks, hermetic, no network):
`esc()` itself escapes `& < > "`; the card template has all six spots
`esc()`ed with no raw `${i.name}` / `${i.ip}` / `${i.status}` /
`${i.cpus}` / `${i.memory_mb}` left; usage line, `nodeFetchBadge`
title, fleet-create label and Nodes-modal attributes all `esc()`ed
with their raw forms gone; the `innerHTML` census (exactly the 23
known lines, every known site present — a new sink fails closed until
classified); no `document.write` / `insertAdjacentHTML` / `outerHTML`
anywhere in the page; negative proof (reverting one `esc()` or adding
a new sink fails the corresponding check).

## Deploy

No standalone step: run `pending-deploy/apply-all/deploy-all.sh`
from an SSH-capable session (or `sync-newbox.sh --yes` for the new
box). Verify after deploy: the live dashboard root still returns 200
and the served `index.html` contains `esc(i.name)` (the card fix is
in the served file). The fix itself is proven by the hermetic suite
above.

## Blocked on

VPS SSH in worker sessions (same as the rest of the queue).
