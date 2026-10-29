"""Unit tests for the staged remote-node Roblox auto-login deploy
(wing/agent.py 1.0.2 + manager/app.py remote login routing).

Run: python3 test_remote_autologin.py
Covers what DEPLOY.md claims was tested on 2026-10-06 ~18:15 UTC
(original /tmp copy lost to /tmp eviction; rebuilt here).
"""
import base64
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

agent = load("staged_agent", os.path.join(HERE, "agent.py"))
app = load("staged_manager_app", os.path.join(HERE, "app.py"))

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

print("== agent 1.0.2+: text input sanitization ==")
check("VERSION >= 1.0.2 (1.0.3 adds node self-watchdog, 1.0.4 adds per-instance resources, 1.0.5 adds drift guard, 1.0.6 adds adaptive screencap, 1.0.7 adds node disk/log guard, 1.0.9 adds selfwatch-events endpoint)",
      agent.VERSION in ("1.0.2", "1.0.3", "1.0.4", "1.0.5", "1.0.6", "1.0.7", "1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"))

s = agent.sanitize_input_text("hello world")
check("spaces -> %s", s == "hello%sworld")

# eval-safety: the input FIFO is eval'd by sh, so nothing shell-dangerous
# may survive sanitization.
evil = agent.sanitize_input_text("a; rm -rf / `id` $(whoami) | tee x & \"q\" 'p' < >")
danger = set(";$`|&<>()\"'\\")
check("no shell metachars survive", not (set(evil) & danger))
check("spaces encoded", " " not in evil)

pwd = agent.sanitize_input_text("P@ss!w0rd_-.,?")
check("allowed special chars kept", pwd == "P@ss!w0rd_-.,?")

check("length capped at 200", len(agent.sanitize_input_text("x" * 300)) == 200)

# base64 transport round-trip (what the /input handler does with b64:true)
secret = "pa$$w0rd!;`evil`"
b64 = base64.b64encode(secret.encode()).decode()
raw = base64.b64decode(b64).decode(errors="replace")
check("b64 round-trip recovers input", raw == secret)
txt = agent.sanitize_input_text(raw)
check("sanitized b64-decoded text is eval-safe",
      not (set(txt) & danger) and " " not in txt)

print("== agent: normalized tap coords scale to verified 1024x608 points ==")
def scale(x, y):
    return (int(float(x) / 1000 * agent.DISP_W), int(float(y) / 1000 * agent.DISP_H))
check("username field (500,375) -> (512,228)", scale(500, 375) == (512, 228))
check("password field (500,487) -> (512,296)", scale(500, 487) == (512, 296))
check("Sign In (500,604) -> (512,367)", scale(500, 604) == (512, 367))

print("== manager: version gate ==")
v = app._version_at_least
check("1.0.2 >= 1.0.2", v("1.0.2", "1.0.2"))
check("1.0.1 < 1.0.2", not v("1.0.1", "1.0.2"))
check("1.1.0 >= 1.0.2", v("1.1.0", "1.0.2"))
check("empty version rejected", not v("", "1.0.2"))
check("garbage version rejected", not v("abc", "1.0.2"))

print("== manager: _roblox_login_remote with mocked network ==")
calls = []
def fake_wing_request(p, method, path, timeout=10, data=None):
    calls.append((method, path))
    if path == "/api/agent/health":
        return 200, {"version": "1.0.38"}
    if path.endswith("/status"):
        return 200, {"running": True}
    return 200, {"ok": True}

inputs = []
def fake_wing_input(p, name, payload, timeout=15):
    inputs.append(payload)
    return True, {"ok": True}

app.wing_request = fake_wing_request
app._wing_input = fake_wing_input
app.load_providers = lambda: {"vultr": {"url": "http://x", "key": "k"}}
app.time.sleep = lambda s: None

ok, msg = app._roblox_login_remote("vultr", "srt1", "someuser", "s3cret!;`x`")
check("happy path ok", ok)
check("message invites verification", "check if login succeeded" in msg)

paths = [p for _, p in calls]
check("health checked first", paths[0] == "/api/agent/health")
check("status checked", any(p.endswith("/status") for p in paths))
check("launch-roblox called", any(p.endswith("/launch-roblox") for p in paths))

expected_types = ["tap", "text", "tap", "text", "tap"]
check("input sequence tap/text/tap/text/tap",
      [p.get("type") for p in inputs] == expected_types)
check("5 inputs sent", len(inputs) == 5)

# tap coords are normalized 0-1000; agent scales them to the verified points
taps = [(p["x"], p["y"]) for p in inputs if p["type"] == "tap"]
check("normalized taps scale to (512,228)/(512,296)/(512,367)",
      [scale(x, y) for x, y in taps] == [(512, 228), (512, 296), (512, 367)])

texts = [p for p in inputs if p["type"] == "text"]
check("username sent as plain text", texts[0]["text"] == "someuser")
check("password sent base64-only",
      texts[1].get("b64") is True
      and base64.b64decode(texts[1]["text"]).decode() == "s3cret!;`x`")
all_text = " ".join(str(p.get("text", "")) for p in inputs)
check("raw password never appears on the wire", "s3cret!;`x`" not in all_text)

print("== manager: old-agent guard ==")
calls.clear(); inputs.clear()
def fake_old(p, method, path, timeout=10, data=None):
    if path == "/api/agent/health":
        return 200, {"version": "1.0.37"}
    return 200, {"running": True}
app.wing_request = fake_old
ok, msg = app._roblox_login_remote("vultr", "srt1", "u", "p")
check("old agent refused", not ok)
check("message names the fix", "too old for auto-login" in msg and "1.0.38" in msg)
check("no inputs sent to old agent", inputs == [])

print("== manager: missing provider / stopped instance ==")
app.wing_request = fake_wing_request
app.load_providers = lambda: {}
ok, msg = app._roblox_login_remote("nope", "x", "u", "p")
check("unknown node -> 'node not found'", (not ok) and msg == "node not found")
app.load_providers = lambda: {"vultr": {"url": "http://x", "key": "k"}}
def fake_stopped(p, method, path, timeout=10, data=None):
    if path == "/api/agent/health":
        return 200, {"version": "1.0.38"}
    return 200, {"running": False}
app.wing_request = fake_stopped
ok, msg = app._roblox_login_remote("vultr", "srt1", "u", "p")
check("stopped instance refused", (not ok) and msg == "instance not running")

print(f"\n{ PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
