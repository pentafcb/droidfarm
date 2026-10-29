"""Unit tests for /api/login rate limiting (manager/app.py + dashboard 429 UX).

Run: python3 test_login_ratelimit.py
Covers:
  server helpers (login_allowed / record_login_failure / clear_login_failures /
    _prune_login_attempts): fresh IP allowed, 9-failures-ok / 10-failures-blocked
    boundary, sliding-window expiry, per-IP isolation, success clears the
    counter, unknown-IP clear is safe, never-raises on garbage input, prune
    drops expired entries and enforces the IP cap stalest-first.
  route contract (/api/login block): login_allowed is consulted before the
    password check, 429 on exceeded budget, record_login_failure on the 401
    path, clear_login_failures on the success path, 401 preserved for bad
    passwords, budget keyed on the client IP.
  dashboard contract: doLogin() has an explicit 429 branch BEFORE the 401
    check (a 429 body has no token, so without the branch it would read as
    "Wrong password."), with a wait-a-minute message and early return.

Hermetic: app.py loaded via importlib (PASSWORD monkeypatched, never the real
one; LOGIN_ATTEMPTS reset between tests); no network, no docker.
"""
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

app = load("loginratelimit_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
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

REAL_PW = app.PASSWORD  # never asserted on, never printed
app.PASSWORD = "t3st-pw"

def reset():
    app.LOGIN_ATTEMPTS.clear()

def fail_n(ip, n):
    for _ in range(n):
        app.record_login_failure(ip)

# ---------------- server: helper behavior ----------------
reset()
check("helper: fresh IP allowed", app.login_allowed("10.0.0.1") is True)

reset()
fail_n("10.0.0.2", 9)
check("helper: 9 failures still allowed", app.login_allowed("10.0.0.2") is True)

reset()
fail_n("10.0.0.3", 10)
check("helper: 10 failures blocked", app.login_allowed("10.0.0.3") is False)

reset()
fail_n("10.0.0.4", 25)
check("helper: 25 failures stays blocked", app.login_allowed("10.0.0.4") is False)

reset()
now = time.time()
app.LOGIN_ATTEMPTS["10.0.0.5"] = [now - 61] * 10  # all expired
check("helper: expired timestamps do not count", app.login_allowed("10.0.0.5") is True)

reset()
now = time.time()
app.LOGIN_ATTEMPTS["10.0.0.6"] = [now - 59] * 9 + [now - 61]  # 9 fresh, 1 expired
check("helper: mixed fresh/expired counts only fresh (allowed)",
      app.login_allowed("10.0.0.6") is True)
app.LOGIN_ATTEMPTS["10.0.0.6"] = [now - 59] * 10 + [now - 61]
check("helper: mixed fresh/expired counts only fresh (blocked)",
      app.login_allowed("10.0.0.6") is False)

reset()
fail_n("10.0.0.7", 10)
check("helper: per-IP isolation (other IP unaffected)",
      app.login_allowed("10.0.0.8") is True and app.login_allowed("10.0.0.7") is False)

reset()
fail_n("10.0.0.9", 10)
check("helper: blocked before clear", app.login_allowed("10.0.0.9") is False)
app.clear_login_failures("10.0.0.9")
check("helper: clear_login_failures resets a blocked IP",
      app.login_allowed("10.0.0.9") is True)

reset()
app.clear_login_failures("10.9.9.9")  # unknown IP
check("helper: clear on unknown IP is safe", app.login_allowed("10.9.9.9") is True)

for weird in (None, 123, b"\x00", ["x"], {"ip": "x"}):
    try:
        app.login_allowed(weird)
        app.record_login_failure(weird)
        app.clear_login_failures(weird)
        ok = True
    except Exception:
        ok = False
    check(f"helper: never raises on garbage ip ({type(weird).__name__})", ok)
reset()

reset()
now = time.time()
app.LOGIN_ATTEMPTS["10.0.0.10"] = [now - 120, now - 61]  # fully expired
app.LOGIN_ATTEMPTS["10.0.0.11"] = [now - 10]             # fresh
app._prune_login_attempts(now)
check("helper: prune drops expired-only IP, keeps fresh",
      "10.0.0.10" not in app.LOGIN_ATTEMPTS and "10.0.0.11" in app.LOGIN_ATTEMPTS)

reset()
old_cap = app.LOGIN_RATE_MAX_IPS
app.LOGIN_RATE_MAX_IPS = 5
now = time.time()
for i in range(8):
    app.LOGIN_ATTEMPTS[f"192.168.9.{i}"] = [now - i]  # staggered freshness
app.login_allowed("192.168.9.200")  # triggers the cap prune
check("helper: IP cap enforced (dict bounded)",
      len(app.LOGIN_ATTEMPTS) <= 5)
check("helper: cap evicts stalest first, keeps freshest",
      "192.168.9.0" in app.LOGIN_ATTEMPTS and "192.168.9.7" not in app.LOGIN_ATTEMPTS)
app.LOGIN_RATE_MAX_IPS = old_cap
reset()

check("helper: constants sane (10/min window 60s)",
      app.LOGIN_RATE_MAX == 10 and app.LOGIN_RATE_WINDOW == 60
      and app.LOGIN_RATE_MAX_IPS >= 64)

# a realistic attack-then-legit sequence
reset()
fail_n("203.0.113.5", 10)
blocked = not app.login_allowed("203.0.113.5")
app.clear_login_failures("203.0.113.5")  # legit user finally gets it right
check("helper: attack blocked, then success clears",
      blocked and app.login_allowed("203.0.113.5") is True)

# ---------------- route contract (/api/login block) ----------------
start = APP_SRC.index('if path == "/api/login":')
nxt = APP_SRC.index('if path == "/api/providers/register":', start)
login_block = APP_SRC[start:nxt]

check("route: login_allowed consulted", "login_allowed(" in login_block)
check("route: budget checked BEFORE the password check",
      login_block.index("login_allowed(") < login_block.index("check_dashboard_password("))
check("route: 429 on exceeded budget",
      "_json(429" in login_block and "too many attempts" in login_block)
check("route: record_login_failure on the 401 path",
      "record_login_failure(" in login_block
      and login_block.index("record_login_failure(") < login_block.index('_json(401'))
check("route: clear_login_failures on the success path",
      "clear_login_failures(" in login_block
      and login_block.index("clear_login_failures(") < login_block.index('_json(200'))
check("route: 401 preserved for bad passwords", '_json(401, {"error": "bad password"})' in login_block)
check("route: budget keyed on client IP",
      "client_address[0]" in login_block
      and login_block.index("client_address[0]") < login_block.index("login_allowed("))
check("route: failures counted only after a real password verdict",
      login_block.index("check_dashboard_password(") < login_block.index("record_login_failure("))

# ---------------- dashboard contract ----------------
d0 = DASH_SRC.index("async function doLogin()")
d1 = DASH_SRC.index("async function updateHostStats()", d0)
do_login = DASH_SRC[d0:d1]

check("dash: explicit 429 branch", "r.status === 429" in do_login)
p429 = do_login.index("r.status === 429")
p401 = do_login.index("r.status === 401")
check("dash: 429 branch BEFORE the 401 check (else reads as wrong password)", p429 < p401)
seg429 = do_login[p429:p401]
import re as _re
_msg429 = _re.search(r"textContent = '([^']*)'", seg429)
_msg429 = _msg429.group(1) if _msg429 else ""
check("dash: 429 message tells the user to wait",
      _re.search(r"wait|espera", _msg429, _re.I) is not None
      and "wrong password" not in _msg429.lower()
      and "contraseña incorrecta" not in _msg429.lower())
check("dash: 429 branch returns early", "return;" in seg429)
check("dash: 401 wrong-password message intact",
      _re.search(r"wrong password|contraseña incorrecta", do_login, _re.I) is not None)

print(f"\n{ PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
