"""Unit tests for wing-selfwatch-dashboard (agent 1.0.9 + manager + dashboard).

The wing agent's selfwatch log (recoveries, DEGRADED trips, disk prunes,
log trims, drift alerts on remote nodes) was invisible: the dashboard
"Eventos" modal only showed the main-node watchdog log. Now:
  agent 1.0.9: GET /api/agent/selfwatch-events?lines=N (authed via
    X-Agent-Key) tails the fixed selfwatch.log path; lines returned raw,
    the manager applies its canonical severity tagging.
  manager: /api/watchdog-events merges local events with per-node
    selfwatch events (attempts=1 fail-fast), tags each with "node", merges
    chronologically by the [YYYY-MM-DD HH:MM:SS] log prefix, and reports
    the contributing "nodes" list.
  dashboard: Eventos modal renders a node chip per node event
    (textContent-only), meta line shows the node count.

Hermetic: wing/agent.py and manager/app.py loaded via importlib; fake
selfwatch.log files; fake wing_request. No network, no docker.

Run: python3 test_wing_selfwatch_dashboard.py
"""
import importlib.util
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("swdash_agent", os.path.join(REPO, "wing", "agent.py"))
app = load("swdash_app", os.path.join(REPO, "manager", "app.py"))
with open(os.path.join(REPO, "manager", "static", "index.html")) as f:
    DASH_SRC = f.read()
with open(os.path.join(REPO, "wing", "agent.py")) as f:
    AGENT_SRC = f.read()
with open(os.path.join(REPO, "manager", "app.py")) as f:
    APP_SRC = f.read()

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def tmpdir():
    return tempfile.mkdtemp(prefix="swdash-")


def write_log(d, lines, binary=False):
    p = os.path.join(d, "selfwatch.log")
    with open(p, "wb") as f:
        for ln in lines:
            f.write(ln if isinstance(ln, bytes) else ln.encode())
    return p


# ---------------- A. agent read_selfwatch_events ----------------
print("== A. agent read_selfwatch_events ==")
d = tmpdir()
lns = ["[2026-10-12 07:%02d:00] event-%d\n" % (i, i) for i in range(10)]
write_log(d, lns)
r = agent.read_selfwatch_events(3, st_dir=d)
check("tail last-N", r["ok"] and len(r["events"]) == 3 and
      r["events"][0]["line"] == "[2026-10-12 07:07:00] event-7",
      repr(r)[:120])
r = agent.read_selfwatch_events(0, st_dir=d)
check("lines=0 clamps to 1", r["ok"] and len(r["events"]) == 1, repr(r)[:80])
r = agent.read_selfwatch_events(9999, st_dir=d)
check("lines huge clamps to 200", r["ok"] and len(r["events"]) == 10,
      repr(r)[:80])  # file has only 10
d2 = tmpdir()
write_log(d2, ["[2026-10-12 07:00:00] %s\n" % ("x" * 300) for _ in range(5)])
r = agent.read_selfwatch_events(9999, st_dir=d2)
check("clamp to 200 with 5 lines", r["ok"] and len(r["events"]) == 5,
      repr(r)[:80])
r = agent.read_selfwatch_events("abc", st_dir=d)
check("garbage lines -> default 100", r["ok"] and len(r["events"]) == 10,
      repr(r)[:80])
check("missing log -> ok False", agent.read_selfwatch_events(10, st_dir=tmpdir())
      == {"ok": False, "events": []})
dd = tmpdir()
os.mkdir(os.path.join(dd, "selfwatch.log"))
check("directory as log -> ok False, never raises",
      agent.read_selfwatch_events(10, st_dir=dd) == {"ok": False, "events": []})
d3 = tmpdir()
write_log(d3, [b"[2026-10-12 07:00:00] bad-\xff\xfe-bytes\n"])
r = agent.read_selfwatch_events(10, st_dir=d3)
check("binary garbage decodes, never raises",
      r["ok"] and len(r["events"]) == 1 and "bad-" in r["events"][0]["line"],
      repr(r)[:120])
d4 = tmpdir()
write_log(d4, ["[2026-10-12 07:00:00] " + "L" * 700 + "\n"])
r = agent.read_selfwatch_events(10, st_dir=d4)
check("line truncated to 500 chars", r["ok"] and len(r["events"][0]["line"]) == 500,
      str(len(r["events"][0]["line"])))
d5 = tmpdir()
big = "".join("[2026-10-12 07:%02d:00] filler-%d\n" % (i % 60, i) for i in range(4000))
write_log(d5, [big])
r = agent.read_selfwatch_events(400, st_dir=d5)
ev = r["events"]
check(">64KB: first line is a complete line (partial dropped)",
      r["ok"] and len(ev) == 200 and ev[0]["line"].startswith("[2026-10-12 07:"),
      repr(ev[0]["line"])[:80] if ev else "empty")
check("event dict shape is line-only (manager tags sev)",
      r["ok"] and set(ev[0].keys()) == {"line"}, repr(set(ev[0].keys())))
check("default st_dir missing -> ok False, never raises",
      agent.read_selfwatch_events(10) == {"ok": False, "events": []})
check("fixed log path discipline in source",
      'os.path.join(st_dir, "selfwatch.log")' in AGENT_SRC, "")
check("VERSION is 1.0.9+ (1.0.10 adds thread supervisor)", agent.VERSION in ("1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), repr(agent.VERSION))

# ---------------- B. agent route contract ----------------
print("== B. agent /api/agent/selfwatch-events route ==")
check("route present in do_GET",
      'if path == "/api/agent/selfwatch-events":' in AGENT_SRC, "")
m_get = re.search(r"def do_GET\(self\):.*?(?=\n    def do_POST)", AGENT_SRC, re.S)
check("do_GET block found", bool(m_get), "")
if m_get:
    body = m_get.group(0)
    auth_pos = body.find("_check_auth()")
    route_pos = body.find('"/api/agent/selfwatch-events"')
    check("route is auth-gated (_check_auth 401/429 gate precedes it)",
          auth_pos != -1 and auth_pos < route_pos
          and '"denied"' in body[:route_pos]
          and '401, {"error": "unauthorized"}' in body[:route_pos], "")
    check("lines query param passed through",
          'read_selfwatch_events(qs.get("lines", "50"))' in body, "")
    check("route before the final 404",
          body.find('"/api/agent/selfwatch-events"') <
          body.rfind('{"error": "not found"}'), "")

# ---------------- C. manager _event_ts ----------------
print("== C. manager _event_ts ==")
exp = time.mktime(time.strptime("2026-10-12 07:45:01", "%Y-%m-%d %H:%M:%S"))
check("valid prefix parses",
      app._event_ts("[2026-10-12 07:45:01] something") == exp, "")
check("missing bracket -> None", app._event_ts("no prefix here") is None, "")
check("short line -> None", app._event_ts("[short]") is None, "")
check("malformed date -> None",
      app._event_ts("[2026-99-99 99:99:99] x") is None, "")
check("non-str -> None", app._event_ts(None) is None and
      app._event_ts(123) is None, "")
try:
    app._event_ts(object())
    check("garbage object never raises", True, "")
except Exception as e:  # noqa: BLE001
    check("garbage object never raises", False, repr(e))

# ---------------- D. fetch_node_watchdog_events ----------------
print("== D. manager fetch_node_watchdog_events ==")

REAL_WR = app.wing_request


class FakeWR:
    def __init__(self):
        self.calls = []  # (pid_url, path, kwargs)

    def __call__(self, provider, method, path, data=None, **kw):
        self.calls.append((provider.get("url"), method, path, dict(kw)))
        return self.resp


def mkprov(pid, url="http://10.0.0.2:8090", name="srt",
           last_seen=None, agent_key="k"):
    return {pid: {"url": url, "name": name, "agent_key": agent_key,
                  "last_seen": last_seen if last_seen is not None
                  else time.time()}}


fake = FakeWR()
app.wing_request = fake
try:
    # offline node: skipped before any request
    fake.resp = (200, {"ok": True, "events": [{"line": "x"}]})
    fake.calls.clear()
    out = app.fetch_node_watchdog_events(
        {"p1": {"url": "http://x:8090", "name": "srt", "agent_key": "k",
                "last_seen": 0}})
    check("stale-heartbeat node skipped (no request)",
          out == [] and fake.calls == [], repr(out))

    # healthy old-agent node: 404 -> contributes nothing
    fake.resp = (404, {"error": "not found"})
    fake.calls.clear()
    out = app.fetch_node_watchdog_events(mkprov("p1"))
    check("old agent 404 contributes nothing", out == [],
          repr(out))
    check("fail-fast contract: attempts=1, timeout=5",
          fake.calls and fake.calls[0][3].get("attempts") == 1
          and fake.calls[0][3].get("timeout") == 5, repr(fake.calls))

    # unreachable: 502 -> nothing
    fake.resp = (502, {"error": "node unreachable"})
    out = app.fetch_node_watchdog_events(mkprov("p1"))
    check("502 contributes nothing", out == [], repr(out))

    # ok=False -> nothing
    fake.resp = (200, {"ok": False, "events": []})
    out = app.fetch_node_watchdog_events(mkprov("p1"))
    check("ok=False contributes nothing", out == [], repr(out))

    # happy path: tagging + node name
    fake.resp = (200, {"ok": True, "events": [
        {"line": "[2026-10-12 07:45:01] ALERT helper restarted"},
        {"line": "[2026-10-12 07:46:02] recovered from DEGRADED"},
        {"line": "plain note"}]})
    out = app.fetch_node_watchdog_events(mkprov("p1", name="srt-wing"))
    check("3 events, node tagged, ts parsed",
          len(out) == 3 and all(e["node"] == "srt-wing" for e in out)
          and out[0]["ts"] == exp and out[2]["ts"] is None,
          repr(out)[:200])
    check("sev tagged by canonical function (alert)",
          out[0]["sev"] == "alert", repr(out[0]))
    check("sev good-news ordering (recovered -> ok)",
          out[1]["sev"] == "ok", repr(out[1]))
    check("untagged line -> info", out[2]["sev"] == "info", repr(out[2]))

    # name fallbacks + truncation
    fake.resp = (200, {"ok": True, "events": [{"line": "x"}]})
    out = app.fetch_node_watchdog_events(mkprov("p1", name=None))
    check("missing name falls back to pid",
          out and out[0]["node"] == "p1", repr(out))
    out = app.fetch_node_watchdog_events(mkprov("p1", name="n" * 100))
    check("node name truncated to 40", out and len(out[0]["node"]) == 40,
          repr(out))

    # garbage payload shapes tolerated
    for bad in [{"events": "nope"}, {"events": [{"line": 42}, "str", None,
                 {"line": "ok"}]}, "not-a-dict"]:
        fake.resp = (200, bad)
        out = app.fetch_node_watchdog_events(mkprov("p1"))
        check("garbage payload tolerated: %r" % (str(bad)[:30],),
              all(isinstance(e["line"], str) for e in out), repr(out))

    # long line truncated
    fake.resp = (200, {"ok": True,
                       "events": [{"line": "y" * 700}]})
    out = app.fetch_node_watchdog_events(mkprov("p1"))
    check("long line truncated to 500",
          out and len(out[0]["line"]) == 500, repr(out))

    # lines clamp
    fake.calls.clear()
    app.fetch_node_watchdog_events(mkprov("p1"), lines="abc")
    check("garbage lines -> 50 in query",
          fake.calls and "lines=50" in fake.calls[0][2], repr(fake.calls))
    fake.calls.clear()
    app.fetch_node_watchdog_events(mkprov("p1"), lines=0)
    check("lines=0 -> 1 in query",
          fake.calls and "lines=1" in fake.calls[0][2], repr(fake.calls))

    # raising wing_request -> never raises, contributes nothing
    def boom(*a, **k):
        raise RuntimeError("net down")
    app.wing_request = boom
    try:
        out = app.fetch_node_watchdog_events(mkprov("p1"))
        check("raising transport never raises", out == [], repr(out))
    finally:
        app.wing_request = fake
    # non-dict provider skipped
    out = app.fetch_node_watchdog_events({"p1": "garbage", "p2": None})
    check("non-dict providers skipped", out == [], repr(out))
    # multi-node: all fetched
    fake.resp = (200, {"ok": True, "events": [{"line": "e"}]})
    fake.calls.clear()
    out = app.fetch_node_watchdog_events(
        dict(mkprov("p1", name="n1"), **mkprov("p2", name="n2")))
    check("two nodes both fetched",
          sorted(e["node"] for e in out) == ["n1", "n2"] and len(fake.calls) == 2,
          repr(out))
finally:
    app.wing_request = REAL_WR

# ---------------- E. merge_watchdog_events ----------------
print("== E. manager merge_watchdog_events ==")
loc = [{"line": "[2026-10-12 07:40:00] local-a", "sev": "info", "node": None,
        "ts": app._event_ts("[2026-10-12 07:40:00] local-a")},
       {"line": "[2026-10-12 07:50:00] local-b", "sev": "warn", "node": None,
        "ts": app._event_ts("[2026-10-12 07:50:00] local-b")}]
nd = [{"line": "[2026-10-12 07:45:00] node-x", "sev": "alert", "node": "srt",
       "ts": app._event_ts("[2026-10-12 07:45:00] node-x")}]
m = app.merge_watchdog_events(loc, nd)
check("chronological merge (oldest first)",
      [e["line"] for e in m] == ["[2026-10-12 07:40:00] local-a",
                                 "[2026-10-12 07:45:00] node-x",
                                 "[2026-10-12 07:50:00] local-b"],
      repr([e["line"] for e in m]))
check("ts stripped from output", all("ts" not in e for e in m),
      repr(m)[:120])
check("node preserved", m[1]["node"] == "srt" and m[0]["node"] is None,
      repr(m)[:120])
m2 = app.merge_watchdog_events(
    [{"line": "no-ts local", "sev": "info", "node": None, "ts": None}],
    [{"line": "[2026-10-12 07:00:00] t", "sev": "info", "node": "srt",
      "ts": app._event_ts("[2026-10-12 07:00:00] t")}])
check("None-ts trails", m2[0]["node"] == "srt" and m2[1]["line"] == "no-ts local",
      repr(m2))
m3 = app.merge_watchdog_events([{"line": "l1", "sev": "info", "node": None, "ts": None}],
                                [{"line": "n1", "sev": "info", "node": "srt", "ts": None}])
check("stable order on equal/missing ts (locals first)",
      [e["line"] for e in m3] == ["l1", "n1"], repr(m3))
check("empty inputs -> []", app.merge_watchdog_events([], []) == [], "")
check("garbage never raises",
      isinstance(app.merge_watchdog_events(None, [None, "x", 42]), list), "")

# ---------------- F. route contract ----------------
print("== F. /api/watchdog-events route contract ==")
m_route = re.search(r'if path == "/api/watchdog-events":.*?(?=\n        # ----)',
                    APP_SRC, re.S)
check("route block found", bool(m_route), "")
if m_route:
    body = m_route.group(0)
    check("401 before any read",
          body.find("401") < body.find("read_watchdog_events"), "")
    check("lines passthrough to local read",
          'read_watchdog_events(qs.get("lines", "100"))' in body, "")
    check("node fetch wired",
          "fetch_node_watchdog_events(" in body and
          "load_providers()" in body, "")
    check("merge wired", "merge_watchdog_events(" in body, "")
    check("response carries nodes list",
          '"nodes": nodes' in body, "")
    check("local ok=False returned as-is (no merge)",
          "if not local.get(\"ok\"):" in body, "")
    check("merged list truncated to requested lines",
          "[-n:]" in body, "")

# ---------------- G. dashboard contract ----------------
print("== G. dashboard Eventos modal contract ==")
check("Eventos modal still present", 'id="eventsModal"' in DASH_SRC, "")
check("node chip rendered with ev-node class",
      "chip.className = 'ev-node'" in DASH_SRC, "")
check("chip uses textContent (no innerHTML injection of node)",
      re.search(r"chip\.textContent\s*=\s*e\.node", DASH_SRC) is not None, "")
check("line rendered via createTextNode (textContent-only preserved)",
      "document.createTextNode(e.line)" in DASH_SRC, "")
check("no innerHTML of event content",
      not re.search(r"innerHTML.*e\.(line|node)", DASH_SRC), "")
check("sev whitelist intact",
      "(['alert','warn','ok','info'].includes(e.sev)" in DASH_SRC, "")
check("meta shows node count",
      re.search(r"j\.nodes.*nodo", DASH_SRC) is not None, "")
check("ev-node CSS defined", ".ev-node" in DASH_SRC, "")
check("Spanish modal title intact", "Eventos del sistema" in DASH_SRC, "")
check("sin datos fallback intact", "Sin datos" in DASH_SRC, "")
check("15s auto-refresh intact",
      "setInterval(loadEvents, 15000)" in DASH_SRC, "")
check("timer cleared on close", "clearInterval(eventsTimer)" in DASH_SRC, "")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
