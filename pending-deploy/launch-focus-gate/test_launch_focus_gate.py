"""Unit tests for the launch focus gate (manager/app.py + wing/agent.py).

Gap: openViewer() fires POST /api/instances/<name>/launch-roblox on EVERY
viewer open, and the fleet "Roblox all" job fires it on every instance —
and launch_roblox() / launch_roblox_linux() tapped blindly (HOME + drawer
swipe + icon tap at 129,424). On a healthy instance the in-container
keepalive already keeps Roblox focused, so opening the viewer to WATCH a
running game yanked the user out of it: HOME exits the game, the drawer
tap relaunches Roblox cold, and the session the user wanted to watch was
gone. The tap was redundant as well as destructive.

Fix: both launch paths probe focus first (dumpsys window's mCurrentFocus,
the same probe the keepalive uses) and only tap when the probe answers
"not focused":
  - focused -> success, zero taps (viewer shows "Roblox abierto", truly).
  - not focused -> the unchanged tap sequence.
  - probe unknown (error, timeout, invalid name) -> failure, zero taps.
    A tap on an unknown-but-focused session is exactly the
    session-destroying case, and a skipped launch is retryable from the
    viewer Roblox button or "Roblox all". This deliberately diverges
    from the keepalive, which taps on probe error: the keepalive retries
    every 10s as recovery, while a dashboard launch is a single user
    action where a false failure is safe and a false tap is not.
  - the agent bumps 1.0.40 -> 1.0.41 so nodes self-update to the gate.

Run: python3 test_launch_focus_gate.py

Hermetic: the real app.py / agent.py modules are loaded; waydroid_exec,
docker, _send_input_cmds, and os.open are faked per case (scoped,
restored after each call). No docker, no network, no real files.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..", "..")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("launch_gate_app", os.path.join(REPO, "manager", "app.py"))
agent = load("launch_gate_agent", os.path.join(REPO, "wing", "agent.py"))

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


APP_SRC = open(os.path.join(REPO, "manager", "app.py")).read()
AGENT_SRC = open(os.path.join(REPO, "wing", "agent.py")).read()
INDEX_SRC = open(os.path.join(REPO, "manager", "static",
                              "index.html")).read()

FOCUSED_DUMP = (
    "  mCurrentFocus=Window{abc u0 com.roblox.client/"
    "com.roblox.client.MainActivity}\n"
    "  mFocusedApp=AppWindowToken{def com.roblox.client}\n"
)
UNFOCUSED_DUMP = (
    "  mCurrentFocus=Window{abc u0 com.android.launcher/"
    "com.android.launcher.Launcher}\n"
)
TAP_CMDS = ["input keyevent 3", "sleep 1",
            "input swipe 512 400 512 100", "sleep 2",
            "input tap 129 424"]

# ---------- manager: roblox_focused ----------


def fake_probe(rc, out):
    calls = []

    def probe(name, cmd, timeout=None):
        calls.append((name, cmd, timeout))
        if isinstance(rc, Exception):
            raise rc
        return rc, out

    probe.calls = calls
    return probe


orig_exec = app.waydroid_exec
try:
    app.waydroid_exec = fake_probe(0, FOCUSED_DUMP)
    check("mgr focused dump -> True", app.roblox_focused("rbx1") is True)
    check("mgr probe asks dumpsys window",
          app.waydroid_exec.calls == [("rbx1", "dumpsys window", 15)])

    app.waydroid_exec = fake_probe(0, UNFOCUSED_DUMP)
    check("mgr unfocused dump -> False",
          app.roblox_focused("rbx1") is False)

    app.waydroid_exec = fake_probe(0, "no focus lines here\n")
    check("mgr rc0 without mCurrentFocus -> False (keepalive fallthrough)",
          app.roblox_focused("rbx1") is False)

    app.waydroid_exec = fake_probe(
        0, "  mCurrentFocus=Window{launcher}\n"
           "  mCurrentFocus=Window{com.roblox.client/x}\n")
    check("mgr first mCurrentFocus wins (launcher first -> False)",
          app.roblox_focused("rbx1") is False)

    app.waydroid_exec = fake_probe(1, "")
    check("mgr probe rc!=0 -> None", app.roblox_focused("rbx1") is None)
    app.waydroid_exec = fake_probe(124, "")
    check("mgr probe timeout 124 -> None",
          app.roblox_focused("rbx1") is None)
    app.waydroid_exec = fake_probe(RuntimeError("boom"), "")
    check("mgr probe raising -> None (never raises)",
          app.roblox_focused("rbx1") is None)

    app.waydroid_exec = fake_probe(0, FOCUSED_DUMP)
    for bad in ("../x", "", "a b", "x" * 33, "rbx1;rm"):
        check(f"mgr invalid name {bad!r} -> None, probe never called",
              app.roblox_focused(bad) is None
              and app.waydroid_exec.calls == [])
finally:
    app.waydroid_exec = orig_exec

# ---------- manager: launch_roblox gating ----------

orig_send = app._send_input_cmds
try:
    sent = []

    def fake_send(name, cmds):
        sent.append((name, list(cmds)))
        return True

    app._send_input_cmds = fake_send

    app.waydroid_exec = fake_probe(0, FOCUSED_DUMP)
    sent.clear()
    check("mgr launch when focused -> True, zero taps",
          app.launch_roblox("rbx1") is True and sent == [])

    app.waydroid_exec = fake_probe(0, UNFOCUSED_DUMP)
    sent.clear()
    check("mgr launch when unfocused -> True, exact tap sequence",
          app.launch_roblox("rbx1") is True
          and sent == [("rbx1", TAP_CMDS)])

    def failing_send(name, cmds):
        sent.append((name, list(cmds)))
        return False

    app._send_input_cmds = failing_send
    sent.clear()
    check("mgr launch unfocused + send failing -> False, taps attempted",
          app.launch_roblox("rbx1") is False and len(sent) == 1)

    app._send_input_cmds = fake_send
    for rc, label in ((1, "rc!=0"), (124, "timeout")):
        app.waydroid_exec = fake_probe(rc, "")
        sent.clear()
        check(f"mgr launch probe {label} -> False, zero taps",
              app.launch_roblox("rbx1") is False and sent == [])

    app.waydroid_exec = fake_probe(0, FOCUSED_DUMP)
    sent.clear()
    check("mgr launch invalid name -> False, probe never called",
          app.launch_roblox("../x") is False
          and app.waydroid_exec.calls == [])
finally:
    app._send_input_cmds = orig_send
    app.waydroid_exec = orig_exec

# ---------- agent: roblox_focused_linux + launch gate ----------

orig_docker = agent.docker
orig_open, orig_write, orig_close = os.open, os.write, os.close
try:
    dcalls = []

    def fake_docker(*args, timeout=None):
        dcalls.append((args, timeout))
        return fake_docker.rc, fake_docker.out

    fake_docker.rc, fake_docker.out = 0, FOCUSED_DUMP
    agent.docker = fake_docker
    check("agent focused dump -> True",
          agent.roblox_focused_linux("rbx1") is True)
    check("agent probe is list-form docker exec dumpsys window",
          dcalls == [(("exec", "wd-rbx1", "waydroid", "shell",
                       "dumpsys", "window"), 15)])
    check("agent VERSION bumped to 1.0.45", agent.VERSION == "1.0.45")

    fake_docker.out = UNFOCUSED_DUMP
    check("agent unfocused dump -> False",
          agent.roblox_focused_linux("rbx1") is False)
    fake_docker.rc, fake_docker.out = 124, ""
    check("agent probe timeout -> None",
          agent.roblox_focused_linux("rbx1") is None)
    fake_docker.rc, fake_docker.out = 1, ""
    check("agent probe rc!=0 -> None",
          agent.roblox_focused_linux("rbx1") is None)
    dcalls.clear()
    check("agent invalid name -> None, docker never called",
          agent.roblox_focused_linux("../x") is None and dcalls == [])

    # Launch gate: focused -> True with no fifo touch; unknown -> False.
    written = []

    def fake_open(path, flags):
        written.append(path)
        return 99

    def fake_write(fd, data):
        written.append(data)
        return len(data)

    os.open, os.write, os.close = fake_open, fake_write, lambda fd: None
    fake_docker.rc, fake_docker.out = 0, FOCUSED_DUMP
    written.clear()
    check("agent launch when focused -> True, fifo untouched",
          agent.launch_roblox_linux("rbx1") is True and written == [])
    fake_docker.rc, fake_docker.out = 1, ""
    written.clear()
    check("agent launch probe unknown -> False, fifo untouched",
          agent.launch_roblox_linux("rbx1") is False and written == [])

    # Unfocused -> compound write reaches the fifo with the exact taps.
    fake_docker.rc, fake_docker.out = 0, UNFOCUSED_DUMP
    agent._find_fifo = lambda cname: "/fake/input.fifo"
    written.clear()
    ok = agent.launch_roblox_linux("rbx1")
    compound = "; ".join(TAP_CMDS) + "\n"
    check("agent launch when unfocused -> True, exact compound fifo write",
          ok is True and written == ["/fake/input.fifo",
                                     compound.encode()])
finally:
    agent.docker = orig_docker
    os.open, os.write, os.close = orig_open, orig_write, orig_close
    if hasattr(agent, "_find_fifo"):
        pass  # _find_fifo reassignment above only affects this process

# ---------- contracts: no behavior change elsewhere ----------

check("mgr tap sequence unchanged in source",
      all(c in APP_SRC for c in TAP_CMDS))
check("agent tap sequence unchanged in source",
      all(c in AGENT_SRC for c in TAP_CMDS))
check("mgr launch-roblox route still delegates to launch_roblox",
      'action == "launch-roblox"' in APP_SRC
      and "ok = launch_roblox(name)" in APP_SRC)
check("dashboard viewer-open still auto-launches (now safely gated)",
      "checkRbx();" in INDEX_SRC)
check("dashboard Roblox button intact",
      "$('btnRbx').onclick = checkRbx;" in INDEX_SRC)
check("login tap path untouched by the gate (separate function)",
      "def roblox_login" in APP_SRC)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
