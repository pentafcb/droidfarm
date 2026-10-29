"""Unit tests for wing agent update-available surfacing in the Nodes modal.

Gap: the dashboard's Nodes modal showed each wing node's agent version but
never said whether it was CURRENT. The vultr installer re-run has been
pending since agent 1.0.2 with the only reminder a PROGRESS.md line -- nobody
watching the dashboard could see the node was 19 versions behind.

Covers: _parse_agent_version units, agent_update_available contract
(True/False/None), current_agent_version disk-read (the deployed
wing/agent.py, same relative layout in repo and on the VPS -- never
hard-coded, so a VERSION bump can never go stale), the /api/providers
wiring contract (agent_latest + agent_update per provider, None for the
local pseudo-provider), and the dashboard contract (badge in the Nodes
modal, update line in the Info modal, Spanish, esc()-safe).

Run: python3 test_wing_agent_update.py

Hermetic: only reads the repo tree (wing/agent.py for the real VERSION);
no network, no docker, no writes.
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
DASH = os.path.join(REPO, "manager", "static", "index.html")
AGENT = os.path.join(REPO, "wing", "agent.py")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("wau_app", APP)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name, detail)


# ---------- A. _parse_agent_version ----------
check("A1 dotted", app._parse_agent_version("1.0.21") == (1, 0, 21))
check("A2 short", app._parse_agent_version("1.0.4") == (1, 0, 4))
check("A3 garbage", app._parse_agent_version("garbage") is None)
check("A4 empty", app._parse_agent_version("") is None)
check("A5 none", app._parse_agent_version(None) is None)
check("A6 local", app._parse_agent_version("local") is None)
check("A7 v-prefix", app._parse_agent_version("v1.2") == (1, 2))
check("A8 whitespace", app._parse_agent_version(" 1.0.21 ") == (1, 0, 21))
check("A9 never-raises", app._parse_agent_version(["x"]) is None)

# ---------- B. agent_update_available ----------
T = app.agent_update_available
check("B1 older -> True", T("1.0.4", "1.0.21") is True)
check("B2 equal -> False", T("1.0.21", "1.0.21") is False)
check("B3 newer -> False", T("1.0.22", "1.0.21") is False)
check("B4 garbage agent -> None", T("garbage", "1.0.21") is None)
check("B5 empty latest -> None", T("1.0.4", "") is None)
check("B6 empty agent -> None", T("", "1.0.21") is None)
check("B7 local pseudo -> None", T("local", "1.0.21") is None)
check("B8 minor compare", T("1.0.19", "1.0.20") is True)
check("B9 never-raises", T(["x"], ["y"]) is None)

# ---------- C. current_agent_version disk read ----------
with tempfile.TemporaryDirectory() as td:
    f = os.path.join(td, "agent.py")
    with open(f, "w") as fh:
        fh.write('"""x"""\nVERSION = "9.9.9"\n')
    check("C1 fake file", app.current_agent_version(_path=f) == "9.9.9")
    f2 = os.path.join(td, "agent2.py")
    with open(f2, "w") as fh:
        fh.write("VERSION = '8.7.6'\n")
    check("C2 single quotes", app.current_agent_version(_path=f2) == "8.7.6")
    f3 = os.path.join(td, "agent3.py")
    with open(f3, "w") as fh:
        fh.write("x = 1\n")
    check("C3 no VERSION line", app.current_agent_version(_path=f3) == "")
    check("C4 missing file", app.current_agent_version(_path=os.path.join(td, "nope.py")) == "")

# the real repo agent.py: must match the source of truth
src_ver = None
with open(AGENT, encoding="utf-8") as fh:
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', fh.read(), re.M)
    src_ver = m.group(1) if m else ""
check("C5 real file parses", src_ver != "")
check("C6 default path == source of truth",
      app.current_agent_version() == src_ver,
      "got %r want %r" % (app.current_agent_version(), src_ver))

# ---------- D. /api/providers wiring contract ----------
import inspect
src = inspect.getsource(app.Handler.do_GET)
check("D1 agent_latest in provider dict", '"agent_latest": latest_agent' in src)
check("D2 agent_update in provider dict",
      '"agent_update": agent_update_available(p.get("version", ""), latest_agent)' in src)
check("D3 computed once before loop",
      re.search(r'latest_agent = current_agent_version\(\)\s*\n\s*for pid, p in providers', src) is not None)
check("D4 local pseudo null + latest", '"agent_update": None' in src and src.count('"agent_latest": latest_agent') == 2)

# ---------- E. dashboard contract ----------
with open(DASH, encoding="utf-8") as fh:
    dash = fh.read()

m = re.search(r"const upd = .*?;\n", dash)
check("E1 upd const exists", m is not None)
expr = m.group(0) if m else ""
check("E2 strict true check", "p.agent_update === true" in expr)
check("E3 local excluded", "p.id !== 'local'" in expr)
check("E4 versions escaped", expr.count("esc(") >= 2)
check("E5 spanish label", "actualización del agente" in expr)
check("E6 tooltip installer", "instalador del nodo" in expr)
check("E7 warn color", "var(--warn)" in expr)
check("E8 badge rendered", "+ upd +" in dash)

check("E9 info modal line", "'Actualización del agente:'" in dash or "Actualización del agente:</b>" in dash)
check("E10 info modal gated", re.search(r"p\.agent_update === true \? '<b>Actualización del agente", dash) is not None)

# no raw version interpolation without esc
check("E11 no unescaped version", "p.version || '?') +" not in dash or "esc(p.version" in dash)

# ---------- F. node-driven: the upd expression evaluates safely ----------
if m:
    js = r"""
const esc = s => String(s||'').replace(/[&<>"]/g, c =>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const p = JSON.parse(process.argv[1]);
%s
console.log(upd);
""" % expr
    r = subprocess.run(["node", "-e", js,
                        '{"id":"n1","version":"1.0.4","agent_latest":"1.0.21","agent_update":true}'],
                       capture_output=True, text=True, timeout=15)
    check("F1 node renders badge", r.returncode == 0 and "1.0.4" in r.stdout and "1.0.21" in r.stdout, r.stderr[:200])
    r2 = subprocess.run(["node", "-e", js,
                         '{"id":"n1","version":"1.0.21","agent_latest":"1.0.21","agent_update":false}'],
                        capture_output=True, text=True, timeout=15)
    check("F2 current agent -> empty", r2.returncode == 0 and r2.stdout.strip() == "", r2.stderr[:200])
    r3 = subprocess.run(["node", "-e", js,
                         '{"id":"local","version":"local","agent_latest":"1.0.21","agent_update":null}'],
                        capture_output=True, text=True, timeout=15)
    check("F3 local -> empty", r3.returncode == 0 and r3.stdout.strip() == "", r3.stderr[:200])
    r4 = subprocess.run(["node", "-e", js,
                         '{"id":"n1","version":"1.0.4<script>","agent_latest":"1.0.21","agent_update":true}'],
                        capture_output=True, text=True, timeout=15)
    check("F4 xss escaped", r4.returncode == 0 and "<script>" not in r4.stdout and "&lt;script&gt;" in r4.stdout, r4.stderr[:200])
else:
    for n in ("F1", "F2", "F3", "F4"):
        check(n + " skipped (no expr)", False, "E1 failed")

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
