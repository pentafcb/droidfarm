"""Unit tests for the route instance-name gate (shell-injection guard).

Gap: six authenticated manager routes passed the raw URL path segment
straight into shell=True docker commands as root with no membership or
shape check:

  GET  /api/instances/<name>/roblox        -> check_roblox -> waydroid_exec
  POST /api/instances/<name>/roblox-login  -> container_status -> docker_inspect
  POST /api/instances/<name>/input         -> do_instance_input -> container_status
  DELETE /api/instances/<name>             -> run("docker stop wd-<name>")
  GET  /ws/screen/<name>                   -> container_status
  GET  /ws/h264/<name>                     -> container_status

A name like `;id>/tmp/x;#` sailed through the ([^/]+) route regex into
`docker inspect wd-;id>/tmp/x;#` via subprocess shell=True -- authenticated
root RCE on the internet-facing manager. instance_single_action was safe
(dict-membership 404), the rest were not.

Fix: valid_instance_name() (^ [A-Za-z0-9_-]{1,32}$, the exact create-time
contract) gates every route capture -> 404 "instance not found",
indistinguishable from an unknown instance. docker_inspect and
waydroid_exec gate as defense in depth (wd-<name> / <name> shapes) so a
hand-edited instances.json can never inject either.

Covers: helper units (incl. never-raises on hostile input), sink gates
(docker_inspect/waydroid_exec refuse hostile names without spawning a
subprocess -- the exploit regression), create-contract parity (the gate
cannot 404 a legitimately-created instance), and source contracts that
every route capture site carries the gate.

Run: python3 test_route_name_gate.py

Hermetic: app.py loaded via importlib; subprocess.run monkeypatched, never
a real shell; no network, no docker.
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("routenamegate_app", APP)
with open(APP) as f:
    APP_SRC = f.read()

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


# ---------------------------------------------------------------- helper units
check("valid: rbx1", app.valid_instance_name("rbx1") is True)
check("valid: srt-20", app.valid_instance_name("srt-20") is True)
check("valid: a_9-x", app.valid_instance_name("a_9-x") is True)
check("valid: 32 chars", app.valid_instance_name("a" * 32) is True)
check("invalid: empty", app.valid_instance_name("") is False)
check("invalid: None", app.valid_instance_name(None) is False)
check("invalid: non-string", app.valid_instance_name(["rbx1"]) is False)
check("invalid: 33 chars", app.valid_instance_name("a" * 33) is False)
check("invalid: semicolon", app.valid_instance_name("rbx1;id") is False)
check("invalid: dollar-paren", app.valid_instance_name("rbx1$(id)") is False)
check("invalid: backtick", app.valid_instance_name("rbx1`id`") is False)
check("invalid: pipe", app.valid_instance_name("rbx1|id") is False)
check("invalid: ampersand", app.valid_instance_name("rbx1&id") is False)
check("invalid: space", app.valid_instance_name("rbx 1") is False)
check("invalid: dotdot", app.valid_instance_name("..") is False)
check("invalid: unicode", app.valid_instance_name("rbx\u00e91") is False)
check("invalid: newline", app.valid_instance_name("rbx1\nid") is False)
# never-raises on hostile input
try:
    app.valid_instance_name(object())
    app.valid_instance_name(b"\xff\xfe")
    check("never-raises on hostile input", True)
except Exception:
    check("never-raises on hostile input", False)

# ------------------------------------------------------- sink gates (exploit)
calls = []


def fake_run(cmd, timeout=30):
    calls.append(cmd)
    return 0, "[]", ""


def fake_run_bytes(cmd, timeout=30):
    calls.append(cmd)
    return 0, b""


orig_run, orig_run_bytes = app.run, app.run_bytes
app.run, app.run_bytes = fake_run, fake_run_bytes
try:
    # docker_inspect: hostile cname -> None, no subprocess
    calls.clear()
    check("docker_inspect refuses metachars",
          app.docker_inspect("wd-rbx1;id>/tmp/x") is None and calls == [])
    calls.clear()
    check("docker_inspect refuses dollar-paren",
          app.docker_inspect("wd-rbx1$(id)") is None and calls == [])
    calls.clear()
    check("docker_inspect refuses bare name",
          app.docker_inspect("rbx1") is None and calls == [])
    calls.clear()
    check("docker_inspect refuses empty",
          app.docker_inspect("") is None and calls == [])
    # docker_inspect: legitimate cname still flows through
    calls.clear()
    out = app.docker_inspect("wd-rbx1")
    check("docker_inspect passes valid cname",
          out is None and len(calls) == 1 and calls[0].startswith("docker inspect wd-rbx1 "))
    # waydroid_exec: hostile name -> (1, ""), no subprocess
    calls.clear()
    check("waydroid_exec refuses metachars",
          app.waydroid_exec("rbx1;id", "getprop foo") == (1, "") and calls == [])
    calls.clear()
    check("waydroid_exec refuses backticks",
          app.waydroid_exec("rbx1`id`", "getprop foo") == (1, "") and calls == [])
    calls.clear()
    check("waydroid_exec refuses None",
          app.waydroid_exec(None, "getprop foo") == (1, "") and calls == [])
    # waydroid_exec: legitimate name still builds the docker commands
    # (waydroid-exec-cleanup: setup + exec + post-run rm of the staged
    # script — the third call is the cleanup, not a second exec).
    calls.clear()
    app.waydroid_exec("rbx1", "getprop sys.boot_completed", timeout=5)
    check("waydroid_exec passes valid name",
          len(calls) == 3 and all("wd-rbx1" in c for c in calls)
          and "rbx1;" not in " ".join(calls)
          and "rm -f" in calls[2] and "cmd-" in calls[2])
finally:
    app.run, app.run_bytes = orig_run, orig_run_bytes

# --------------------------------------- create-contract parity (no false 404)
# The create endpoints sanitize with this exact expression; every non-empty
# result must pass the route gate, or the gate would 404 real instances.
CREATE_SANITIZE = lambda raw: re.sub(r"[^a-zA-Z0-9_-]", "", str(raw or ""))[:32]
samples = ["rbx1", "srt-20", "a" * 40, "my instance!", "rbx;1", "..", "",
           "UPPER_case-9", "x" * 32, "x" * 33]
parity = all((not CREATE_SANITIZE(s)) or app.valid_instance_name(CREATE_SANITIZE(s))
             for s in samples)
check("create-sanitizer output always passes the gate", parity)
# fleet-create generates prefix+N with a sanitized prefix (<=24 chars)
check("fleet-style names pass",
      all(app.valid_instance_name(f"rbx{i}") for i in (1, 9, 20, 199)))

# ------------------------------------------------- route source contracts
# Every `name = unquote(m.group(1))` (instance-name capture) must be
# followed by the gate before any use. pid/tok captures are dict-ops only
# and intentionally ungated.
lines = APP_SRC.splitlines()
http_sites = 0
for i, ln in enumerate(lines):
    if re.match(r"^\s*name = unquote\(m\.group\(1\)\)$", ln):
        http_sites += 1
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "if not valid_instance_name(name):" not in nxt:
            check(f"gate after capture (line {i + 1})", False)
            break
else:
    check(f"all {http_sites} HTTP captures gated", http_sites == 11)
    if http_sites != 11:
        check("expected exactly 11 HTTP capture sites", False)

# the gate returns 404 "instance not found" (no oracle vs unknown instance)
gate_404s = len(re.findall(
    r'if not valid_instance_name\(name\):\s*\n\s*return self\._json\(404, \{"error": "instance not found"\}\)',
    APP_SRC))
check("HTTP gates answer 404 instance-not-found", gate_404s == 11)

# ws handlers: unquote(name) -> gate -> 404 bytes
for fn in ("_handle_ws", "_handle_ws_h264"):
    src_fn = re.search(rf"def {fn}\(self, name\):(.*?)(?=\n    def |\Z)",
                       APP_SRC, re.S).group(1)
    check(f"{fn} gates name",
          "name = unquote(name)" in src_fn
          and "if not valid_instance_name(name):" in src_fn
          and 'self._send(404, b"instance not found")' in src_fn)

# sink-gate contracts in source
check("docker_inspect source gate",
      're.fullmatch(r"wd-[A-Za-z0-9_-]{1,32}", name or "")' in APP_SRC)
check("waydroid_exec source gate",
      re.search(r"def waydroid_exec\(.*?\):\s*\n(\s*\"\"\".*?\"\"\"\s*\n)?\s*# Sink gate",
                APP_SRC, re.S) is not None
      and "if not valid_instance_name(name):" in APP_SRC)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
