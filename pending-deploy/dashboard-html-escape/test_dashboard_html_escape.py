#!/usr/bin/env python3
"""Tests for dashboard-html-escape (manager/static/index.html only).

GAP (security, not a user report): the dashboard renders server data
through innerHTML, and most sinks already escaped their values (badges,
provider names, bulk-import results, install logs all go through esc())
— but the instance CARD did not: the card template interpolated
i.name, i.ip, i.status (twice: class attribute + text), i.cpus and
i.memory_mb RAW, plus i.memory_mb raw in the usage line, the fleet
node-failure count raw in the nodeFetchBadge title, p.total raw in the
fleet-create button label, and p.id raw in the Nodes-modal data-info /
data-del attributes. Today those values are server-shaped (instance
names are create-time-validated, provider ids are server-generated
p_hex), so nothing exploitable was live — but the card is the one
place a corrupted record, a future field, or a hostile node agent could
have turned into stored HTML injection in an authenticated dashboard
session, and the class-attribute interpolation (dot ${i.status}) is
exactly the shape that breaks out of an attribute.

FIX (manager/static/index.html only, no agent/manager/wire change):
every one of those interpolations now goes through esc(), which escapes
& < > " — enough for both the text nodes and the double-quoted
attributes used here (data-info/data-del are double-quoted; esc covers
the quote). Valid data renders byte-identically; only markup in a
value changes (it now shows literally instead of parsing).

Covers:
  A. esc() itself escapes & < > " (the function the fix relies on)
  B. card template regression: all six raw spots now esc()ed, no raw
     ${i.name}/${i.ip}/${i.status}/${i.cpus}/${i.memory_mb} left
  C. usage line, nodeFetchBadge title, fleet-create label, Nodes-modal
     attributes: esc()ed, raw forms gone
  D. innerHTML census: exactly the 23 known innerHTML lines, every
     known site present — a new sink fails closed until classified
  E. no other HTML sinks (document.write / insertAdjacentHTML /
     outerHTML) anywhere in the page
  F. negative proof: doctored copies (one esc() reverted, one new sink
     added) fail the corresponding checks — the gate is not vacuous
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PAGE = os.path.join(REPO, "manager", "static", "index.html")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def check(name, cond, detail=""):
    if cond:
        ok(name)
    else:
        bad(name, detail)


def line_with(src, needle):
    for ln in src.splitlines():
        if needle in ln:
            return ln
    return ""


# Checks are functions of source text so the negative-proof section can
# run them against doctored copies.
def check_esc_fn(src):
    m = re.search(r"function esc\(s\)\{([^}]*)\}", src)
    if not m:
        return False
    body = m.group(1)
    return all(t in body for t in ("&amp;", "&lt;", "&gt;", "&quot;"))


def check_card(src):
    ln = line_with(src, "d.innerHTML = `")
    if not ln:
        return False
    if ln.count("esc(i.status)") < 2:
        return False
    for expr in ("esc(i.name)", "esc(i.ip", "esc(i.cpus)",
                 "esc(i.memory_mb)"):
        if expr not in ln:
            return False
    for raw in ("${i.name}", "${i.ip", "${i.status}", "${i.cpus}",
                "${i.memory_mb}"):
        if raw in ln:
            return False
    return True


def check_usage(src):
    ln = line_with(src, "usageHtml = `")
    return ("esc(i.memory_mb)" in ln and "${i.memory_mb}" not in ln)


def check_fetch_badge(src):
    ln = line_with(src, "nodeFetchBadge = `")
    return ("esc(i.node_fetch_failures" in ln
            and "${i.node_fetch_failures" not in ln)


def check_fleet_button(src):
    ln = line_with(src, "btnDoFleetCreate').innerHTML")
    return "esc(p.total)" in ln


def check_prov_attrs(src):
    info = line_with(src, "data-info=")
    dele = line_with(src, "data-del=")
    if "esc(p.id)" not in info or "esc(p.id)" not in dele:
        return False
    # Double-quoted attributes: the value sits between " chars, and esc()
    # covers " — a raw + p.id + would leave the attribute open.
    return ("+ p.id +" not in info and "+ p.id +" not in dele)


def innerhtml_lines(src):
    return [ln for ln in src.splitlines() if ".innerHTML" in ln]


KNOWN_SITES = [
    "g.innerHTML = ''",
    "d.innerHTML = `",
    "sel.innerHTML = '<option",
    "$('fleetFails').innerHTML = ''",
    "fleetFails').innerHTML = '<p",
    "fleetFails').innerHTML = h",
    "fcPlan').innerHTML = ''",
    "fcPlan').innerHTML =",
    "btnDoFleetCreate').innerHTML",
    "acctLoginRes').innerHTML",
    "acctBulkRes').innerHTML = ''",
    "box.innerHTML = ''",
    "acctBulkRes').innerHTML = '<p",
    "acctBulkRes').innerHTML = html",
    "el.innerHTML = ''",
    "div.innerHTML =",
    "piBody').innerHTML =",
    "installLogList').innerHTML",
]


def check_census(src):
    lines = innerhtml_lines(src)
    if len(lines) != 23:
        return False
    joined = "\n".join(lines)
    return all(s in joined for s in KNOWN_SITES)


def check_no_other_sinks(src):
    return ("document.write" not in src
            and "insertAdjacentHTML" not in src
            and "outerHTML" not in src)


def main():
    src = open(PAGE, encoding="utf-8").read()

    # --- A. esc() itself ------------------------------------------------
    check("esc() escapes & < > \"", check_esc_fn(src))
    # --- B. card template ----------------------------------------------
    check("card: all six spots esc()ed, no raw interpolation left",
          check_card(src))
    # --- C. the other four sinks ----------------------------------------
    check("usage line: memory_mb esc()ed", check_usage(src))
    check("nodeFetchBadge title: failure count esc()ed",
          check_fetch_badge(src))
    check("fleet-create button: total esc()ed", check_fleet_button(src))
    check("nodes-modal attrs: p.id esc()ed, no raw + p.id +",
          check_prov_attrs(src))
    # --- D. census -------------------------------------------------------
    check("innerHTML census: 23 lines, all known sites present",
          check_census(src))
    # --- E. no other HTML sinks ------------------------------------------
    check("no document.write/insertAdjacentHTML/outerHTML",
          check_no_other_sinks(src))

    # --- F. negative proof (doctored copies, repo untouched) -----------
    doctored = src.replace("esc(i.name)", "i.name", 1)
    check("negative: reverting esc(i.name) fails the card check",
          not check_card(doctored))
    doctored = src.replace("esc(p.id)", "p.id", 1)
    check("negative: reverting esc(p.id) fails the attrs check",
          not check_prov_attrs(doctored))
    doctored = src + "\n  $('evil').innerHTML = userInput;\n"
    check("negative: a new innerHTML sink fails the census",
          not check_census(doctored))
    doctored = src.replace("esc(i.memory_mb)", "i.memory_mb", 1)
    check("negative: reverting esc(i.memory_mb) fails the usage check",
          not check_usage(doctored))

    print(f"PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
