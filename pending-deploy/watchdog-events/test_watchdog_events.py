"""Unit tests for the watchdog event feed (manager/app.py + dashboard Eventos modal).

Run: python3 test_watchdog_events.py
Covers:
  severity tagging (alert/warn/ok/info + the "recovered from DEGRADED" is
    good-news ordering rule, garbage input never raises),
  read_watchdog_events: tail correctness, line-count clamping [1,200],
    default on garbage, missing file / directory path -> ok False (never
    raises), binary garbage decodes, 500-char line truncation, >64KB
    partial-first-line drop, path echoed in the result, event dict shape,
  route contract: /api/watchdog-events is auth-gated (401 before any read)
    and passes the lines query param through,
  dashboard contract: Eventos button in the fleet bar, eventsModal, fetch
    of /api/watchdog-events?lines=100, textContent rendering (no innerHTML
    injection), sev whitelist, 15s auto-refresh while open, timer cleared
    on close, severity CSS classes.

Hermetic: app.py loaded via importlib; WATCHDOG_LOG monkeypatched to temp
files; no network, no docker.
"""
import importlib.util
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("watchdogevents_app",
           os.path.join(HERE, "..", "..", "manager", "app.py"))
with open(os.path.join(HERE, "..", "..", "manager", "static", "index.html")) as f:
    DASH_SRC = f.read()
with open(os.path.join(HERE, "..", "..", "manager", "app.py")) as f:
    APP_SRC = f.read()

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def tmpfile(content_bytes):
    fd, p = tempfile.mkstemp(prefix="wdevents-")
    with os.fdopen(fd, "wb") as f:
        f.write(content_bytes)
    return p


def lines(n, prefix="L"):
    return "".join(f"{prefix}{i:04d} some event text\n" for i in range(1, n + 1))


sev = app.watchdog_event_severity

# ---- severity ----
check("sev: ALERT -> alert",
      sev("[2026-10-10 01:00:00] ALERT rbx1 Roblox client version DRIFT") == "alert")
check("sev: DEGRADED -> alert",
      sev("[2026-10-10 01:00:00] rbx2 DEGRADED: container restarted 7 times") == "alert")
check("sev: WARN -> warn",
      sev("[2026-10-10 01:00:00] WARN curl missing - manager self-check skipped") == "warn")
check("sev: recovered -> ok",
      sev("[2026-10-10 01:00:00] manager recovered from DEGRADED (HTTP 200 healthy)") == "ok")
check("sev: recovered-from-DEGRADED is good news, not alert",
      sev("[2026-10-10 01:00:00] rbx1 recovered from DEGRADED (all checks green)") == "ok")
check("sev: started ok -> ok",
      sev("[2026-10-10 01:00:00] rbx3 started ok") == "ok")
check("sev: restarted ok -> ok",
      sev("[2026-10-10 01:00:00] manager restarted ok") == "ok")
check("sev: back on pinned -> ok",
      sev("[2026-10-10 01:00:00] rbx4 Roblox client back on pinned build 2.740.931") == "ok")
check("sev: plain line -> info",
      sev("[2026-10-10 01:00:00] rbx5 container down -> docker start") == "info")
check("sev: garbage input never raises, -> info",
      sev(None) == "info" and sev(123) == "info")

# ---- read_watchdog_events ----
p = tmpfile(lines(150).encode())
app.WATCHDOG_LOG = p
r = app.read_watchdog_events(100)
check("tail: 150 lines, ask 100 -> 100 events",
      r["ok"] is True and len(r["events"]) == 100)
check("tail: first returned line is line 51 (oldest dropped)",
      r["events"][0]["line"].startswith("L0051"))
check("tail: last returned line is line 150",
      r["events"][-1]["line"].startswith("L0150"))
check("tail: event dicts carry line + sev",
      all(set(e.keys()) == {"line", "sev"} for e in r["events"]))
os.unlink(p)

p = tmpfile(lines(30).encode())
app.WATCHDOG_LOG = p
r = app.read_watchdog_events(100)
check("tail: fewer lines than asked -> all 30, no padding",
      r["ok"] is True and len(r["events"]) == 30
      and r["events"][0]["line"].startswith("L0001"))
os.unlink(p)

p = tmpfile(lines(10).encode())
app.WATCHDOG_LOG = p
check("clamp: lines=0 -> 1 event",
      len(app.read_watchdog_events(0)["events"]) == 1)
check("clamp: lines=-5 -> 1 event",
      len(app.read_watchdog_events(-5)["events"]) == 1)
os.unlink(p)

p = tmpfile(lines(250).encode())
app.WATCHDOG_LOG = p
r = app.read_watchdog_events(9999)
check("clamp: lines=9999 -> capped at 200",
      len(r["events"]) == 200 and r["events"][0]["line"].startswith("L0051"))
r = app.read_watchdog_events("abc")
check("garbage lines param -> default 100",
      len(r["events"]) == 100)
os.unlink(p)

app.WATCHDOG_LOG = "/nonexistent-dir-xyz/nope.log"
r = app.read_watchdog_events(100)
check("missing file -> ok False, empty events, never raises",
      r["ok"] is False and r["events"] == []
      and r["path"] == "/nonexistent-dir-xyz/nope.log")

d = tempfile.mkdtemp(prefix="wdevents-dir-")
app.WATCHDOG_LOG = d
r = app.read_watchdog_events(10)
check("log path is a directory -> ok False, never raises",
      r["ok"] is False and r["events"] == [])
os.rmdir(d)

p = tmpfile(b"\xff\xfe bad \x80 bytes\n[2026-10-10 01:00:00] ok line\n")
app.WATCHDOG_LOG = p
r = app.read_watchdog_events(10)
check("binary garbage decodes (replace), never raises",
      r["ok"] is True and len(r["events"]) == 2)
os.unlink(p)

p = tmpfile(("x" * 600 + "\n").encode())
app.WATCHDOG_LOG = p
r = app.read_watchdog_events(10)
check("600-char line truncated to 500",
      len(r["events"]) == 1 and len(r["events"][0]["line"]) == 500)
os.unlink(p)

big = "".join(f"L{i:05d} " + "y" * 60 + "\n" for i in range(2000))  # ~140KB
p = tmpfile(big.encode())
app.WATCHDOG_LOG = p
r = app.read_watchdog_events(5)
check(">64KB file: no partial first line (all match full-line pattern)",
      r["ok"] is True and len(r["events"]) == 5
      and all(re.fullmatch(r"L\d{5} y+", e["line"]) for e in r["events"]))
check("result echoes the configured log path",
      r["path"] == app.WATCHDOG_LOG)
os.unlink(p)

# ---- route contract ----
i = APP_SRC.find('"/api/watchdog-events"')
block = APP_SRC[i:i + 500] if i != -1 else ""
check("route: /api/watchdog-events registered in do_GET", i != -1)
check("route: auth gate returns 401 before any log read",
      "_authed()" in block and "401" in block
      and block.find("_authed()") < block.find("read_watchdog_events"))
check("route: lines query param passed through",
      'qs.get("lines"' in block)
check("helpers defined: read_watchdog_events + watchdog_event_severity",
      "def read_watchdog_events(" in APP_SRC
      and "def watchdog_event_severity(" in APP_SRC)

# ---- dashboard contract ----
check("dash: Eventos button in the fleet bar",
      re.search(r'id="btnEvents"', DASH_SRC) is not None
      and DASH_SRC.find('id="btnEvents"') < DASH_SRC.find('id="grid"'))
check("dash: eventsModal exists", 'id="eventsModal"' in DASH_SRC)
check("dash: fetches /api/watchdog-events?lines=100",
      "/api/watchdog-events?lines=100" in DASH_SRC)
check("dash: event lines rendered injection-safe (textContent or createTextNode, no innerHTML)",
      (re.search(r"\.textContent\s*=\s*e\.line", DASH_SRC) is not None
       or re.search(r"createTextNode\(e\.line\)", DASH_SRC) is not None)
      and not re.search(r"\.innerHTML\s*=\s*e\.", DASH_SRC)
      and not re.search(r"\.innerHTML.*e\.line", DASH_SRC))
check("dash: sev whitelist guards the CSS class",
      "['alert','warn','ok','info'].includes(e.sev)" in DASH_SRC)
check("dash: auto-refresh every 15s while open",
      "setInterval(loadEvents, 15000)" in DASH_SRC)
check("dash: close clears the refresh timer",
      "$('btnEventsClose').onclick" in DASH_SRC
      and "clearInterval(eventsTimer)" in DASH_SRC)
check("dash: severity CSS classes defined",
      all(c in DASH_SRC for c in
          (".ev-alert", ".ev-warn", ".ev-ok", ".ev-info")))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
