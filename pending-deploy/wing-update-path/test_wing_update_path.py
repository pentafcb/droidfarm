#!/usr/bin/env python3
"""wing-update-path: the manager must resolve wing/agent.py in BOTH the
deployed (flat) and repo (nested) layouts.

REAL BUG (found 2026-10-23 by auditing the wing self-update chain):
_agent_disk_path() used dirname(dirname(__file__)) + "/wing/agent.py".
That is correct only in the repo (manager/app.py -> repo root -> wing/),
but every VPS deploy is FLAT (/root/jerusalem/app.py, agent staged at
/root/jerusalem/wing/agent.py) -- so on the live box it resolved to
/root/wing/agent.py, which does not exist. Consequence, all silent:
current_agent_version() -> "" and agent_file_sha256() -> "" ->
agent_update_directive() -> None on every heartbeat -> wing nodes NEVER
self-updated, the dashboard "update available" badge never fired, and
/api/agent/update/download 500'd ("agent file unavailable"). Every
PROGRESS.md entry claiming "vultr gets agent 1.0.x via self-update on the
next heartbeat" was false on the live box.

The fix: prefer WING_DIR/agent.py (the same dir the /api/install/agent.py
route serves from, so advertised sha256 can never disagree with served
bytes), fall back to the repo layout.

Hermetic: fake trees for both layouts, module __file__/WING_DIR patched,
version/sha caches reset between cases. Real manager/app.py import.
"""
import hashlib
import importlib.util
import inspect
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("wup_app", APP)

# Capture sources BEFORE __file__ is patched for the layout fixtures --
# inspect resolves source through the module's __file__.
DO_GET_SRC = inspect.getsource(app.Handler.do_GET)
DISK_PATH_SRC = inspect.getsource(app._agent_disk_path)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name, detail)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


# ---------- fixture trees ----------
TMP = tempfile.mkdtemp(prefix="wup-")
# flat/deploy layout: <root>/app.py beside <root>/wing/agent.py
FLAT_APP = os.path.join(TMP, "flat", "app.py")
FLAT_AGENT = os.path.join(TMP, "flat", "wing", "agent.py")
write(FLAT_APP, "# fake flat manager\n")
FLAT_BYTES = b'# fake agent\nVERSION = "9.9.9"\n'
write(FLAT_AGENT, FLAT_BYTES.decode())
FLAT_SHA = hashlib.sha256(FLAT_BYTES).hexdigest()
# nested/repo layout: <root>/manager/app.py, <root>/wing/agent.py
NEST_APP = os.path.join(TMP, "nested", "manager", "app.py")
NEST_AGENT = os.path.join(TMP, "nested", "wing", "agent.py")
write(NEST_APP, "# fake nested manager\n")
write(NEST_AGENT, '# fake agent\nVERSION = "8.8.8"\n')
# precedence tree: deployed exists AND the old double-dirname path exists
# with a DIFFERENT version -- deployed must win.
PREC_APP = os.path.join(TMP, "prec", "app.py")
PREC_AGENT = os.path.join(TMP, "prec", "wing", "agent.py")
write(PREC_APP, "# fake\n")
write(PREC_AGENT, '# fake agent\nVERSION = "7.7.7"\n')
write(os.path.join(TMP, "wing", "agent.py"), '# stale\nVERSION = "1.0.0"\n')

_orig_file = app.__file__
_orig_wingdir = app.WING_DIR


def set_layout(app_file, wing_dir):
    app.__file__ = app_file
    app.WING_DIR = wing_dir


def reset_cache():
    app._CURRENT_AGENT_VERSION = None
    app._CURRENT_AGENT_SHA256 = None


def restore():
    app.__file__ = _orig_file
    app.WING_DIR = _orig_wingdir
    reset_cache()


# ---------- A. path resolution ----------
set_layout(FLAT_APP, os.path.join(TMP, "flat", "wing"))
check("A1 flat layout resolves to WING_DIR/agent.py",
      app._agent_disk_path() == FLAT_AGENT,
      app._agent_disk_path())

set_layout(NEST_APP, os.path.join(TMP, "nested", "manager", "wing"))
check("A2 repo layout falls back to repo-root wing/agent.py",
      app._agent_disk_path() == NEST_AGENT,
      app._agent_disk_path())

set_layout(PREC_APP, os.path.join(TMP, "prec", "wing"))
check("A3 deployed layout wins when both exist",
      app._agent_disk_path() == PREC_AGENT,
      app._agent_disk_path())

set_layout(os.path.join(TMP, "nope", "app.py"),
           os.path.join(TMP, "nope", "wing"))
got = app._agent_disk_path()
check("A4 nothing exists -> repo-layout guess, never raises",
      got == os.path.join(TMP, "wing", "agent.py"), got)

app.WING_DIR = None
app.__file__ = FLAT_APP
try:
    got = app._agent_disk_path()
    ok = isinstance(got, str)
except Exception as e:  # noqa: BLE001
    ok, got = False, repr(e)
check("A5 hostile WING_DIR never raises", ok, got)

# ---------- B. end-to-end on the flat (live-box) layout ----------
set_layout(FLAT_APP, os.path.join(TMP, "flat", "wing"))
reset_cache()
check("B1 flat: current_agent_version reads the deployed file",
      app.current_agent_version() == "9.9.9", app.current_agent_version())
reset_cache()
check("B2 flat: agent_file_sha256 matches the deployed bytes",
      app.agent_file_sha256() == FLAT_SHA, app.agent_file_sha256())
reset_cache()
d = app.agent_update_directive("1.0.4")
check("B3 flat: older agent gets a directive",
      isinstance(d, dict) and d.get("version") == "9.9.9", d)
check("B4 flat: directive sha256 == sha256 of the served path",
      isinstance(d, dict) and d.get("sha256") == hashlib.sha256(
          open(app._agent_disk_path(), "rb").read()).hexdigest())
reset_cache()
check("B5 flat: current agent gets no directive",
      app.agent_update_directive("9.9.9") is None)
reset_cache()
check("B6 flat: garbage agent version -> None, never raises",
      app.agent_update_directive("garbage") is None)

# ---------- C. repo layout still works end-to-end ----------
set_layout(NEST_APP, os.path.join(TMP, "nested", "manager", "wing"))
reset_cache()
check("C1 repo: current_agent_version reads repo wing/agent.py",
      app.current_agent_version() == "8.8.8", app.current_agent_version())
reset_cache()
d2 = app.agent_update_directive("1.0.4")
check("C2 repo: older agent gets a directive",
      isinstance(d2, dict) and d2.get("version") == "8.8.8", d2)

# ---------- D. wiring contracts ----------
check("D1 download route serves _agent_disk_path()",
      "apath = _agent_disk_path()" in DO_GET_SRC)
check("D2 download route headers come from the same path",
      "X-Agent-Version" in DO_GET_SRC and "current_agent_version()" in DO_GET_SRC
      and "X-Agent-Sha256" in DO_GET_SRC and "agent_file_sha256()" in DO_GET_SRC)
check("D3 install route serves WING_DIR/agent.py (agreement)",
      'os.path.join(WING_DIR, "agent.py")' in DO_GET_SRC)
check("D4 no bare double-dirname return remains",
      "os.path.dirname(os.path.dirname" in DISK_PATH_SRC
      and "WING_DIR" in DISK_PATH_SRC)

# ---------- E. the old behavior, proven dead ----------
set_layout(FLAT_APP, os.path.join(TMP, "flat", "wing"))
reset_cache()
old_guess = os.path.join(os.path.dirname(os.path.dirname(FLAT_APP)),
                         "wing", "agent.py")
check("E1 flat: resolved path is NOT the old double-dirname guess",
      app._agent_disk_path() != old_guess, old_guess)
# E2 on a CLEAN flat tree (no planted fallback): the old guess is absent
# on a real flat deploy, and resolution still lands on the deployed file.
TMP2 = tempfile.mkdtemp(prefix="wup2-")
F2_APP = os.path.join(TMP2, "app.py")
F2_AGENT = os.path.join(TMP2, "wing", "agent.py")
write(F2_APP, "# fake\n")
write(F2_AGENT, '# fake\nVERSION = "9.9.8"\n')
set_layout(F2_APP, os.path.join(TMP2, "wing"))
reset_cache()
old2 = os.path.join(os.path.dirname(os.path.dirname(F2_APP)),
                    "wing", "agent.py")
check("E2 flat (clean): old guess absent, deployed file used",
      not os.path.exists(old2) and app._agent_disk_path() == F2_AGENT, old2)
set_layout(FLAT_APP, os.path.join(TMP, "flat", "wing"))
reset_cache()
d3 = app.agent_update_directive("1.0.4")
check("E3 flat: an older agent is now offered the update (was None)",
      isinstance(d3, dict) and d3.get("version") == "9.9.9", d3)

restore()
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
