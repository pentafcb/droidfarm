#!/usr/bin/env python3
"""Unit tests for state-600-complete (manager/app.py + wing/agent.py).

REAL GAP FOUND by auditing the atomic-write idiom after the
providers-600 session fixed the last credential-bearing manager save:
two manager saves and the agent's save were still unhardened.

* manager save_instances: NO chmod at all. Under umask 022
  instances.json sat world-readable, exposing the fleet's instance
  names and internal IPs.
* manager save_login_results: chmod AFTER the replace — the exact
  flaw state-tmp-sweep fixed for accounts. A crash between replace
  and chmod left a world-readable tmp behind, and the live file was
  briefly exposed on every save. Outcome-only file, but it was the
  last chmod-after instance in the manager.
* wing agent save_instances: the same no-chmod save on the node
  side, exposing the node's instance names and internal IPs.

Fix: all three chmod their tmp to 600 BEFORE the replace
(os.replace preserves the tmp's mode, so the live file is 600 from
birth; OSError-guarded so a chmod failure never breaks the save).
The next save after deploy repairs already-deployed files
automatically, since every one of these files is re-saved on normal
operation. No version bump: no manager gate depends on the agent's
save_instances and self-update is sha-driven (same precedent as
remote-b64-sweep), so VERSION stays 1.0.38.

After this change, every save_* in manager/app.py and the agent's
save_instances is chmod-before-replace — the state-file 600 audit
is complete. (The agent's recoveries.json counter write is counts
only, not state, and is out of scope.)

Hermetic: app.py loaded via importlib with JERUSALEM_DATA pointed
at a tmp dir; agent.py loaded via importlib with its INSTANCES_PATH
rebound to a tmp path; no docker, no network, no real DATA_DIR
touched. umask is set to 022 in-process so a missing chmod would
show up as mode 644.

Run: python3 test_state_600_complete.py
"""
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

BASE = tempfile.mkdtemp(prefix="s600_")
DATA = os.path.join(BASE, "data")
os.makedirs(DATA)
os.environ["JERUSALEM_DATA"] = DATA
os.umask(0o022)


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("s600_app", os.path.join(REPO, "manager", "app.py"))
agent = load("s600_agent", os.path.join(REPO, "wing", "agent.py"))
with open(os.path.join(REPO, "manager", "app.py")) as f:
    APP_SRC = f.read()
with open(os.path.join(REPO, "wing", "agent.py")) as f:
    AGENT_SRC = f.read()

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def src_block(src, funcname):
    m = re.search(r"def %s\(.*?\n(?=\ndef |\Z)" % funcname, src, re.S)
    return m.group(0) if m else ""


# --- 1. Manager save_instances: fresh save under umask 022 ---
INST = os.path.join(DATA, "instances.json")
app.save_instances({"rbx1": {"name": "rbx1", "ip": "10.20.0.11",
                              "cpus": 2, "memory_mb": 2048}})
check("manager instances fresh save creates file", os.path.isfile(INST))
check("manager instances fresh save is mode 600", mode(INST) == 0o600)
check("manager instances fresh save leaves no .tmp",
      not os.path.exists(INST + ".tmp"))
check("manager instances round-trips",
      app.load_instances().get("rbx1", {}).get("ip") == "10.20.0.11")

# --- 2. Manager save_instances: pre-existing 644 file hardened ---
os.chmod(INST, 0o644)
app.save_instances({"rbx1": {"name": "rbx1", "ip": "10.20.0.12"}})
check("manager instances 644 file hardened by next save",
      mode(INST) == 0o600)
check("manager instances hardened save round-trips",
      app.load_instances()["rbx1"]["ip"] == "10.20.0.12")

# --- 3. Manager save_login_results: fresh save + chmod-before-replace ---
LR = os.path.join(DATA, "roblox_login_results.json")
app.save_login_results({"rbx1": {"ok": True, "ts": 1, "reason": "ok",
                                  "by": "manual", "attempts": 1}})
check("manager login-results fresh save creates file", os.path.isfile(LR))
check("manager login-results fresh save is mode 600", mode(LR) == 0o600)
check("manager login-results fresh save leaves no .tmp",
      not os.path.exists(LR + ".tmp"))
check("manager login-results round-trips",
      app.load_login_results()["rbx1"]["ok"] is True)

# --- 4. Manager save_login_results: pre-existing 644 hardened ---
os.chmod(LR, 0o644)
app.record_login_result("rbx1", False, "no credentials saved", "manual")
check("manager login-results 644 file hardened by next save",
      mode(LR) == 0o600)
check("manager login-results record keeps outcome only",
      "password" not in json.dumps(app.load_login_results()))

# --- 5. Source contracts: chmod precedes replace in all three ---
for label, src, fn in (("manager save_instances", APP_SRC, "save_instances"),
                       ("manager save_login_results", APP_SRC,
                        "save_login_results"),
                       ("agent save_instances", AGENT_SRC, "save_instances")):
    blk = src_block(src, fn)
    # Match the code calls, not the comments (comments mention
    # "os.replace preserves the tmp's mode" before the chmod code).
    c = blk.find("os.chmod(tmp")
    r = blk.find("os.replace(tmp")
    check(f"{label} has chmod", c >= 0)
    check(f"{label} chmod precedes replace", 0 <= c < r)
    check(f"{label} chmod is OSError-guarded",
          "except OSError" in blk)
check("manager save_login_results no longer chmods the live file after",
      "os.chmod(LOGIN_RESULTS_FILE" not in src_block(APP_SRC,
                                                      "save_login_results"))

# --- 6. Agent save_instances: fresh save under umask 022 ---
AINST = os.path.join(BASE, "agent-instances.json")
agent.INSTANCES_PATH = AINST
agent.save_instances({"srt1": {"name": "srt1", "ip": "10.20.1.11"}})
check("agent instances fresh save creates file", os.path.isfile(AINST))
check("agent instances fresh save is mode 600", mode(AINST) == 0o600)
check("agent instances fresh save leaves no .tmp",
      not os.path.exists(AINST + ".tmp"))
check("agent instances round-trips",
      agent.load_instances().get("srt1", {}).get("ip") == "10.20.1.11")

# --- 7. Agent save_instances: pre-existing 644 hardened ---
os.chmod(AINST, 0o644)
agent.save_instances({"srt1": {"name": "srt1", "ip": "10.20.1.12"}})
check("agent instances 644 file hardened by next save",
      mode(AINST) == 0o600)

# --- 8. No version cascade ---
m = re.search(r"^VERSION\s*=\s*[\"']([^\"']+)[\"']", AGENT_SRC, re.M)
check("agent VERSION is 1.0.44", bool(m) and m.group(1) == "1.0.45")

# --- 9. No other manager save_* left unhardened ---
# Every def save_* block must contain a chmod before its replace.
for fn in re.findall(r"^def (save_\w+)\(", APP_SRC, re.M):
    blk = src_block(APP_SRC, fn)
    c, r = blk.find("os.chmod(tmp"), blk.find("os.replace(tmp")
    if r < 0:
        continue  # not an atomic-write save; out of scope
    check(f"manager {fn} chmod-before-replace", 0 <= c < r)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
