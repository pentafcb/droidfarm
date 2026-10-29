"""Unit tests for providers.json 600 hardening (manager/app.py).

Run: python3 test_providers_600.py

REAL GAP FOUND by auditing the atomic-write idiom after the
state-tmp-sweep session fixed accounts + tokens: save_providers was
the one credential-bearing save never chmodded. providers.json holds
every node's agent_key — the credential the manager presents to wing
agents (X-Agent-Key) and the key agents use to pull self-updates — yet
under umask 022 the live file (and any crashed providers.json.tmp
beside it) sat world-readable. The /api/providers response already
strips agent_key, so the on-disk file was the only leak path; the
state-tmp-sweep startup sweep removed stale tmps but never fixed the
live file's mode, and no save ever chmodded it after.

Fix (manager/app.py only, no agent change, no version cascade):
save_providers chmods its tmp to 600 BEFORE the replace (os.replace
preserves the tmp's mode, so the live file is 600 from birth). The
next heartbeat save after deploy fixes already-deployed files, since
every heartbeat re-saves providers.

Hermetic: app.py loaded via importlib with JERUSALEM_DATA pointed at a
tmp dir; no docker, no network, no real DATA_DIR touched. umask is set
to 022 in-process so a missing chmod would show up as mode 644.
"""
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = tempfile.mkdtemp(prefix="prov600_")
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


app = load("prov600_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
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


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


PROV = os.path.join(DATA, "providers.json")
PROV_TMP = PROV + ".tmp"

# 1. Fresh save under umask 022.
app.save_providers({"p_abc": {"id": "p_abc", "url": "http://10.0.0.2:8090",
                              "agent_key": "SECRETKEY1", "name": "wing"}})
check("fresh save creates providers.json", os.path.isfile(PROV))
check("fresh save is mode 600 under umask 022", mode(PROV) == 0o600)
check("fresh save leaves no .tmp", not os.path.exists(PROV_TMP))
loaded = app.load_providers()
check("round-trip preserves agent_key", loaded.get("p_abc", {}).get("agent_key") == "SECRETKEY1")
check("round-trip preserves url", loaded.get("p_abc", {}).get("url") == "http://10.0.0.2:8090")

# 2. A pre-existing world-readable file is hardened by the next save.
os.chmod(PROV, 0o644)
app.save_providers({"p_abc": {"id": "p_abc", "url": "http://10.0.0.3:8090",
                              "agent_key": "SECRETKEY2"}})
check("pre-existing 644 file becomes 600 on next save", mode(PROV) == 0o600)
check("pre-existing file content updated", app.load_providers()["p_abc"]["url"] == "http://10.0.0.3:8090")

# 3. Overwrite stays 600; multiple providers all preserved.
app.save_providers({"p_abc": {"agent_key": "K1"}, "p_def": {"agent_key": "K2"}})
check("overwrite stays 600", mode(PROV) == 0o600)
check("multi-provider keys preserved",
      app.load_providers().get("p_abc", {}).get("agent_key") == "K1"
      and app.load_providers().get("p_def", {}).get("agent_key") == "K2")

# 4. Empty save is valid JSON and still 600.
app.save_providers({})
with open(PROV) as f:
    empty = json.load(f)
check("empty save is valid {}", empty == {})
check("empty save still 600", mode(PROV) == 0o600)

# 5. Source contracts.
seg = APP_SRC[APP_SRC.index("def save_providers"):]
seg = seg[:seg.index("def gen_secret")]
check("save_providers chmods its tmp to 600", "os.chmod(tmp, 0o600)" in seg)
check("save_providers chmods before the replace",
      seg.index("os.chmod(tmp, 0o600)") < seg.index("os.replace(tmp, PROVIDERS_FILE)"))
check("save_providers chmod failure is tolerated (OSError guard)",
      "except OSError" in seg)

# 6. The dashboard API is not a second leak path for the key.
api_seg = APP_SRC[APP_SRC.index('if path == "/api/providers":'):]
api_seg = api_seg[:api_seg.index("# include local as a pseudo-provider")]
check("/api/providers response never includes agent_key", "agent_key" not in api_seg)

# 7. A chmod failure must not break the save (best-effort, like tokens).
real_chmod = os.chmod
os.chmod = lambda *a, **k: (_ for _ in ()).throw(OSError("mocked"))
try:
    app.save_providers({"p_abc": {"agent_key": "K3"}})
    chmod_fail_ok = os.path.isfile(PROV) and app.load_providers()["p_abc"]["agent_key"] == "K3"
finally:
    os.chmod = real_chmod
check("save still succeeds when chmod fails", chmod_fail_ok)
check("no .tmp left after chmod-failure save", not os.path.exists(PROV_TMP))

# 8. No other state files created as a side effect.
check("only providers.json in the state dir",
      sorted(os.listdir(DATA)) == ["providers.json"])

shutil.rmtree(BASE, ignore_errors=True)
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
