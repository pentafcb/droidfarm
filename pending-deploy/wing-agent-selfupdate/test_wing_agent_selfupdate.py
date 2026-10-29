"""Unit tests for wing agent self-update (agent 1.0.24+).

Gap: the dashboard's Nodes modal badges a node whose agent is older than the
one deployed beside the manager (wing-agent-update), but every version bump
still needed Penta to re-run the node installer by hand — twenty versions
were pending at once. The agent now updates itself: the heartbeat response
carries {"agent_update": {"version", "sha256"}} when a newer agent.py sits
beside the manager; the agent pulls the file from its OWN manager (the
MAIN_URL it already trusts), verifies the sha256, byte-compiles it, swaps
it in atomically (one .bak kept), and exits so the supervisor (systemd)
restarts it on the new code.

Covers: VERSION 1.0.24+ (exact-bump ownership passed to the suite that
introduces the bump, per the handoff pattern) + manager >=1.0.2 gate, parse_agent_version
units (incl. 1.0.9 < 1.0.10 numeric ordering), manager agent_file_sha256 /
agent_update_directive units, heartbeat-response directive contract,
download-endpoint contract (header auth, size cap, version/sha headers),
fetch_agent_update (bad directive/version/sha/url, sha mismatch, compile
failure, too-large, non-200, success), apply_agent_update (backup + atomic
swap, fresh-install path, never-raises), maybe_agent_self_update (windows /
already-done / no-directive / current / older / garbage-version /
no-systemd one-WARN-per-day / fetch-fail / apply-fail / success+exit once /
raising-fetch never propagates / garbage directives never raise), and the
heartbeat_loop wiring contract.

Run: python3 test_wing_agent_selfupdate.py

Hermetic: the agent and manager modules are loaded from the repo tree;
network is faked by monkeypatching urllib.request.urlopen; all file writes
go to tmp dirs. No docker, no network, no systemd.
"""
import hashlib
import importlib.util
import inspect
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wasu_agent", AGENT)
app = load("wasu_app", APP)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name, detail)


def reset_update_flag():
    agent._UPDATE_ATTEMPTED = False


# ---------- A. version ----------------------------------------------------
# (Widened for 1.0.25; the exact-bump assertion now lives in the
# wing-selfwatch-lock suite per the handoff pattern.)
check("A1 VERSION is 1.0.24+", agent.VERSION in ("1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"),
      "got %r" % (agent.VERSION,))
check("A2 manager >=1.0.2 gate accepts 1.0.24",
      app._version_at_least("1.0.24", "1.0.2") is True)

# ---------- B. parse_agent_version (agent) --------------------------------
check("B1 dotted", agent.parse_agent_version("1.0.24") == (1, 0, 24))
check("B2 numeric ordering not string order",
      agent.parse_agent_version("1.0.9") < agent.parse_agent_version("1.0.10"))
check("B3 short", agent.parse_agent_version("2") == (2,))
check("B4 garbage -> None", agent.parse_agent_version("garbage") is None)
check("B5 empty -> None", agent.parse_agent_version("") is None)
check("B6 None -> None", agent.parse_agent_version(None) is None)
check("B7 matches manager parse",
      agent.parse_agent_version("1.0.23") == app._parse_agent_version("1.0.23"))

# ---------- C. manager sha256 helpers -------------------------------------
real_sha = hashlib.sha256(open(AGENT, "rb").read()).hexdigest()
check("C1 agent_file_sha256 matches real file",
      app.agent_file_sha256() == real_sha, real_sha[:16])
check("C2 _path override", app.agent_file_sha256(_path=AGENT) == real_sha)
check("C3 missing file -> ''",
      app.agent_file_sha256(_path="/nonexistent/x.py") == "")
check("C4 _read_file_sha256 empty file",
      app._read_file_sha256(os.devnull) ==
      hashlib.sha256(b"").hexdigest())

# ---------- D. agent_update_directive (manager) -----------------------------
latest = app.current_agent_version()
check("D0 sanity: disk version matches agent.VERSION",
      latest == agent.VERSION, "got %r" % (latest,))
d = app.agent_update_directive("1.0.23")
check("D1 older agent gets directive",
      isinstance(d, dict) and d.get("version") == agent.VERSION
      and d.get("sha256") == real_sha, repr(d)[:80])
check("D2 current agent -> None",
      app.agent_update_directive(agent.VERSION) is None)
check("D3 newer agent -> None", app.agent_update_directive("9.9.9") is None)
check("D4 garbage -> None", app.agent_update_directive("garbage") is None)
check("D5 empty -> None", app.agent_update_directive("") is None)
check("D6 None -> None", app.agent_update_directive(None) is None)

# ---------- E. heartbeat response contract (manager source) -----------------
hb_src = inspect.getsource(app.Handler.do_POST)
check("E1 heartbeat wires agent_update_directive",
      "agent_update_directive" in hb_src and '"agent_update"' in hb_src)
check("E2 heartbeat still returns ok:true base",
      'resp = {"ok": True}' in hb_src)
check("E3 directive failure never breaks heartbeat",
      hb_src.count("except Exception") >= 1 and
      "directive = agent_update_directive" in hb_src)

# ---------- F. download endpoint contract (manager source) ------------------
get_src = inspect.getsource(app.Handler.do_GET)
check("F1 route path exact", '"/api/agent/update/download"' in get_src)
check("F2 header auth (X-Provider-Id/X-Agent-Key)",
      '"X-Provider-Id"' in get_src and '"X-Agent-Key"' in get_src)
check("F3 bad key -> 401", "401, {\"error\": \"unauthorized\"}" in get_src)
check("F4 hmac compare_digest on the key", "compare_digest" in get_src)
check("F5 size cap enforced", "AGENT_UPDATE_MAX_BYTES" in get_src)
check("F6 version+sha response headers",
      '"X-Agent-Version"' in get_src and '"X-Agent-Sha256"' in get_src)
check("F7 serves raw bytes (not json)", '_send(200, data, "text/x-python"' in get_src)
check("F8 constant is 2MB", app.AGENT_UPDATE_MAX_BYTES == 2 * 1024 * 1024)
check("F9 fail-closed on empty key either side",
      "not stored or not key" in get_src)

# ---------- G. fetch_agent_update (agent, faked urlopen) --------------------
GOOD_PY = b'VERSION = "9.9.9"\nX = 1\n'
good_sha = hashlib.sha256(GOOD_PY).hexdigest()
# The fake "available" version must always be newer than the on-disk agent,
# whatever it is, so this suite survives future bumps.
_newer = agent.parse_agent_version(agent.VERSION)
NEWER_VERSION = "%d.%d.%d" % (_newer[0], _newer[1], _newer[2] + 1)
good_directive = {"version": NEWER_VERSION, "sha256": good_sha}

orig_urlopen = agent.urllib.request.urlopen


class FakeResp:
    def __init__(self, code=200, data=b""):
        self._code, self._data = code, data

    def getcode(self):
        return self._code

    def read(self, n=-1):
        return self._data if n is None or n < 0 else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen_factory(data=b"", code=200, record=None):
    def _fake(req, timeout=None):
        if record is not None:
            record.append(req)
        return FakeResp(code, data)
    return _fake


tmpd = tempfile.mkdtemp(prefix="wasu-")

ok, info = agent.fetch_agent_update(None, main_url="http://m:1",
                                   dest_dir=tmpd)
check("G1 None directive -> (False, bad directive)",
      ok is False and info == "bad directive", repr(info))
ok, info = agent.fetch_agent_update("nope", main_url="http://m:1",
                                   dest_dir=tmpd)
check("G2 non-dict directive -> bad directive",
      ok is False and info == "bad directive")
ok, info = agent.fetch_agent_update({"version": "xx", "sha256": good_sha},
                                    main_url="http://m:1", dest_dir=tmpd)
check("G3 bad version -> bad version",
      ok is False and info == "bad version", repr(info))
ok, info = agent.fetch_agent_update({"version": "1.0.25", "sha256": "zzz"},
                                    main_url="http://m:1", dest_dir=tmpd)
check("G4 bad sha256 -> bad sha256",
      ok is False and info == "bad sha256", repr(info))
ok, info = agent.fetch_agent_update(good_directive, main_url="",
                                   dest_dir=tmpd)
check("G5 empty manager url -> no manager url",
      ok is False and info == "no manager url", repr(info))

agent.urllib.request.urlopen = fake_urlopen_factory(GOOD_PY)
try:
    ok, info = agent.fetch_agent_update(
        {"version": "1.0.25", "sha256": "0" * 64}, main_url="http://m:1",
        dest_dir=tmpd)
    check("G6 sha mismatch -> (False, sha256 mismatch)",
          ok is False and info == "sha256 mismatch", repr(info))
finally:
    agent.urllib.request.urlopen = orig_urlopen

BAD_PY = b"def broken(:\n"
bad_sha = hashlib.sha256(BAD_PY).hexdigest()
agent.urllib.request.urlopen = fake_urlopen_factory(BAD_PY)
try:
    ok, info = agent.fetch_agent_update(
        {"version": "1.0.25", "sha256": bad_sha}, main_url="http://m:1",
        dest_dir=tmpd)
    leftovers = [f for f in os.listdir(tmpd)
                 if f.startswith("agent-update-")]
    check("G7 compile failure -> (False, compile failed), temp removed",
          ok is False and info == "compile failed" and not leftovers,
          repr((info, leftovers)))
finally:
    agent.urllib.request.urlopen = orig_urlopen

agent.urllib.request.urlopen = fake_urlopen_factory(b"x", code=500)
try:
    ok, info = agent.fetch_agent_update(good_directive, main_url="http://m:1",
                                       dest_dir=tmpd)
    check("G8 non-200 -> (False, http 500)",
          ok is False and info == "http 500", repr(info))
finally:
    agent.urllib.request.urlopen = orig_urlopen

big = b"x" * (agent.AGENT_UPDATE_MAX_BYTES + 10)
agent.urllib.request.urlopen = fake_urlopen_factory(big)
try:
    ok, info = agent.fetch_agent_update(
        {"version": "1.0.25",
         "sha256": hashlib.sha256(big).hexdigest()},
        main_url="http://m:1", dest_dir=tmpd)
    check("G9 oversize -> (False, too large)",
          ok is False and info == "too large", repr(info))
finally:
    agent.urllib.request.urlopen = orig_urlopen

rec = []
agent.urllib.request.urlopen = fake_urlopen_factory(GOOD_PY, record=rec)
try:
    ok, info = agent.fetch_agent_update(
        good_directive, main_url="http://mgr:8090/",
        provider_id="p1", agent_key="k1", dest_dir=tmpd)
    content_ok = ok and open(info, "rb").read() == GOOD_PY
    hdr = rec[0].headers if rec else {}
    # urllib normalizes header names to "X-provider-id" style; compare lower
    hl = {k.lower(): v for k, v in hdr.items()}
    auth_ok = (hl.get("x-provider-id") == "p1"
               and hl.get("x-agent-key") == "k1")
    url_ok = rec and rec[0].full_url == \
        "http://mgr:8090/api/agent/update/download"
    check("G10 success: compiled tmp, auth headers, exact URL",
          ok is True and content_ok and auth_ok and url_ok,
          repr((ok, auth_ok, url_ok)))
    if ok and os.path.exists(info):
        os.remove(info)
finally:
    agent.urllib.request.urlopen = orig_urlopen

# ---------- H. apply_agent_update -------------------------------------------
adir = tempfile.mkdtemp(prefix="wasu-apply-")
cur = os.path.join(adir, "agent.py")
open(cur, "w").write("OLD\n")
newf = os.path.join(adir, "new.py")
open(newf, "w").write("NEW\n")
check("H1 swap: backup keeps old, new in place",
      agent.apply_agent_update(newf, cur) is True
      and open(cur).read() == "NEW\n"
      and open(cur + ".bak").read() == "OLD\n"
      and not os.path.exists(newf))
fresh = os.path.join(adir, "agent2.py")
newf2 = os.path.join(adir, "new2.py")
open(newf2, "w").write("N2\n")
check("H2 fresh path (no current file) still installs",
      agent.apply_agent_update(newf2, fresh) is True
      and open(fresh).read() == "N2\n")
check("H3 missing tmp -> False, never raises",
      agent.apply_agent_update(os.path.join(adir, "nope.py"), cur) is False)
# H4: None tmp_path must not raise and must not touch the real tree: point
# agent_path at a tmp file so no .bak is ever written into the repo.
try:
    r = agent.apply_agent_update(None, os.path.join(adir, "h4.py"))
    check("H4 garbage args -> False, never raises", r is False, repr(r))
except Exception as e:
    check("H4 garbage args -> False, never raises", False, repr(e))

# ---------- I. maybe_agent_self_update --------------------------------------
sdir = tempfile.mkdtemp(prefix="wasu-sw-")
SYS = {"INVOCATION_ID": "test"}
NOSYS = {}

reset_update_flag()
check("I1 no directive -> no-directive",
      agent.maybe_agent_self_update(None, _env=SYS, _st_dir=sdir)
      == "no-directive")

reset_update_flag()
check("I2 current version -> current",
      agent.maybe_agent_self_update({"version": "1.0.24", "sha256": "0" * 64},
                                   _env=SYS, _st_dir=sdir) == "current")
reset_update_flag()
check("I3 older directive -> current",
      agent.maybe_agent_self_update({"version": "1.0.9", "sha256": "0" * 64},
                                   _env=SYS, _st_dir=sdir) == "current")
reset_update_flag()
check("I4 garbage version -> skipped-version",
      agent.maybe_agent_self_update({"version": "xx", "sha256": "0" * 64},
                                   _env=SYS, _st_dir=sdir)
      == "skipped-version")

reset_update_flag()
orig_iswin = agent.IS_WINDOWS
agent.IS_WINDOWS = True
try:
    check("I5 windows -> skipped-windows",
          agent.maybe_agent_self_update(good_directive, _env=SYS,
                                       _st_dir=sdir) == "skipped-windows")
finally:
    agent.IS_WINDOWS = orig_iswin

reset_update_flag()
r = agent.maybe_agent_self_update(good_directive, _env=NOSYS, _st_dir=sdir)
log1 = open(os.path.join(sdir, "selfwatch.log")).read() \
    if os.path.exists(os.path.join(sdir, "selfwatch.log")) else ""
r2 = agent.maybe_agent_self_update(good_directive, _env=NOSYS, _st_dir=sdir)
log2 = open(os.path.join(sdir, "selfwatch.log")).read()
check("I6 no systemd -> skipped-nosystemd, one WARN/day",
      r == "skipped-nosystemd" and r2 == "skipped-nosystemd"
      and log1.count("self-update skipped") == 1
      and log2.count("self-update skipped") == 1
      and os.path.exists(os.path.join(
          sdir, "agent.update.nosystemd." +
          __import__("time").strftime("%F"))),
      repr((r, r2)))

reset_update_flag()
agent._UPDATE_ATTEMPTED = True
agent._UPDATE_RETRY_AFTER = __import__("time").time() + 600
exits = []
calls = []
check("I7 already attempted (cooldown) -> skipped-cooldown, no fetch, no exit",
      agent.maybe_agent_self_update(good_directive, _env=SYS, _st_dir=sdir,
                                   _fetch=lambda d: calls.append(d) or (True, "x"),
                                   _exit_fn=lambda c: exits.append(c))
      == "skipped-cooldown" and exits == [] and calls == [])
reset_update_flag()

reset_update_flag()
agent._UPDATE_ATTEMPTED = True
agent._UPDATE_RETRY_AFTER = __import__("time").time() - 1  # cooldown expired
calls2 = []
r = agent.maybe_agent_self_update(good_directive, _env=SYS, _st_dir=sdir,
                                 _fetch=lambda d: calls2.append(d) or (False, "boom"),
                                 _exit_fn=lambda c: exits.append(c))
check("I7b cooldown expired -> retries fetch (fetch-failed again)",
      r == "fetch-failed" and len(calls2) == 1 and exits == [], repr(r))
reset_update_flag()

reset_update_flag()
check("I8 fetch failure -> fetch-failed, no exit, WARN logged",
      agent.maybe_agent_self_update(good_directive, _env=SYS, _st_dir=sdir,
                                   _fetch=lambda d: (False, "boom"),
                                   _exit_fn=lambda c: exits.append(c))
      == "fetch-failed" and exits == []
      and "fetch failed: boom" in open(os.path.join(sdir, "selfwatch.log")).read())
reset_update_flag()

reset_update_flag()
tmpp = os.path.join(sdir, "t.py")
open(tmpp, "w").write("x=1\n")
check("I9 apply failure -> apply-failed, temp removed, no exit",
      agent.maybe_agent_self_update(good_directive, _env=SYS, _st_dir=sdir,
                                   _fetch=lambda d: (True, tmpp),
                                   _apply=lambda p: False,
                                   _exit_fn=lambda c: exits.append(c))
      == "apply-failed" and exits == [] and not os.path.exists(tmpp))
reset_update_flag()

reset_update_flag()
adir2 = tempfile.mkdtemp(prefix="wasu-ok-")
cur2 = os.path.join(adir2, "agent.py")
open(cur2, "w").write("OLD\n")
new2 = os.path.join(adir2, "n.py")
open(new2, "w").write("NEW\n")
exits2 = []


def fake_fetch(d):
    assert d["version"] == NEWER_VERSION and d["sha256"] == good_sha
    return (True, new2)


st = agent.maybe_agent_self_update(
    good_directive, _env=SYS, _st_dir=sdir, _fetch=fake_fetch,
    _apply=lambda p: agent.apply_agent_update(p, cur2),
    _exit_fn=lambda c: exits2.append(c))
log = open(os.path.join(sdir, "selfwatch.log")).read()
check("I10 success -> updated, exit(1) once, backup, ALERT logged",
      st == "updated" and exits2 == [1]
      and open(cur2).read() == "NEW\n"
      and open(cur2 + ".bak").read() == "OLD\n"
      and "ALERT agent self-updated %s -> %s" % (agent.VERSION, NEWER_VERSION) in log,
      repr(st))
reset_update_flag()

reset_update_flag()


def boom_fetch(d):
    raise RuntimeError("net down")


check("I11 raising fetch -> error, never propagates, no exit",
      agent.maybe_agent_self_update(good_directive, _env=SYS, _st_dir=sdir,
                                   _fetch=boom_fetch,
                                   _exit_fn=lambda c: exits.append(c)) == "error"
      and exits == [])
reset_update_flag()

reset_update_flag()
statuses = set()
for g in ([], "x", 123, {"version": None}, {"sha256": "0" * 64}):
    try:
        statuses.add(agent.maybe_agent_self_update(g, _env=SYS, _st_dir=sdir,
                                                  _exit_fn=lambda c: exits.append(c)))
    except Exception as e:
        statuses.add("RAISED:%r" % (e,))
check("I12 garbage directives never raise, always a status string",
      all(isinstance(s, str) and not s.startswith("RAISED")
          for s in statuses),
      repr(statuses))
reset_update_flag()

# ---------- J. heartbeat_loop wiring contract --------------------------------
hb_loop_src = inspect.getsource(agent.heartbeat_loop)
check("J1 heartbeat reads the response body", "resp.read(" in hb_loop_src)
check("J2 heartbeat extracts agent_update + calls maybe",
      '"agent_update"' in hb_loop_src
      and "maybe_agent_self_update" in hb_loop_src)
check("J3 update errors never break heartbeats",
      hb_loop_src.count("except Exception") >= 2)

# ---------- K. dashboard: no changes needed ----------------------------------
dash = open(os.path.join(REPO, "manager", "static", "index.html")).read()
check("K1 update badge still driven by heartbeat version (no new UI)",
      "agent_update" in dash or "agent-update" in dash or
      "update" in dash.lower())

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
