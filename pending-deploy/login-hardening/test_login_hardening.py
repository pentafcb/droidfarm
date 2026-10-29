"""Unit tests for dashboard sign-in hardening (manager/app.py /api/login +
manager/static/index.html doLogin/token storage).

Run: python3 test_login_hardening.py
Covers:
  server: check_dashboard_password — correct/wrong ASCII, empty, non-string
    input (None/int/list/dict) never raises, non-ASCII passwords work (the old
    code raised TypeError inside hmac.compare_digest -> HTTP 500 -> dashboard
    showed "Wrong password." forever), long input safe; the /api/login route
    goes through check_dashboard_password and no longer hands raw request data
    to hmac.compare_digest.
  dashboard: token save/load with localStorage -> sessionStorage -> in-memory
    fallback (iOS private mode / quota-exhausted localStorage.setItem throws);
    logout clears both stores; initial token comes from loadToken(); doLogin
    distinguishes three outcomes — connection failure, 401/wrong password,
    and other server errors — instead of blaming every failure on the password.

Hermetic: app.py loaded via importlib (PASSWORD monkeypatched, never the real
one); no network, no docker.
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("loginharden_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
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

# ---------------- server: check_dashboard_password ----------------
REAL_PW = app.PASSWORD  # not asserted on; never printed
app.PASSWORD = "t3st-pw"

check("server: correct ascii password", app.check_dashboard_password("t3st-pw") is True)
check("server: wrong password", app.check_dashboard_password("wrong") is False)
check("server: empty password", app.check_dashboard_password("") is False)
check("server: trailing space differs", app.check_dashboard_password("t3st-pw ") is False)

for bad in (None, 123, 4.5, ["t3st-pw"], {"pw": "t3st-pw"}, b"t3st-pw"):
    try:
        r = app.check_dashboard_password(bad)
        check(f"server: non-str {type(bad).__name__} -> False, no raise", r is False)
    except Exception as e:  # noqa: BLE001
        check(f"server: non-str {type(bad).__name__} raised {e!r}", False)

# Non-ASCII: old code did hmac.compare_digest(str, str) which raises TypeError
# for non-ASCII -> 500. New code must accept a unicode password correctly.
app.PASSWORD = "pässwörd-✓"
check("server: unicode password correct", app.check_dashboard_password("pässwörd-✓") is True)
check("server: unicode password wrong", app.check_dashboard_password("pässwörd-✗") is False)
check("server: ascii guess vs unicode pw", app.check_dashboard_password("passwörd-✓") is False)
app.PASSWORD = "t3st-pw"

check("server: very long input safe", app.check_dashboard_password("x" * 100000) is False)
check("server: returns bool type", isinstance(app.check_dashboard_password("t3st-pw"), bool))

# Route wiring: /api/login must go through the helper, not raw compare_digest.
login_block = APP_SRC[APP_SRC.index('if path == "/api/login"'):APP_SRC.index('if path == "/api/login"') + 900]
check("server: /api/login uses check_dashboard_password",
      "check_dashboard_password(data.get(\"password\"" in login_block)
check("server: /api/login no longer hands raw input to hmac.compare_digest",
      "hmac.compare_digest(data.get" not in login_block)
check("server: still returns 401 bad password", 'return self._json(401, {"error": "bad password"})' in login_block)
check("server: helper defined near PASSWORD", "def check_dashboard_password(pw):" in APP_SRC)

app.PASSWORD = REAL_PW  # restore; never printed

# ---------------- dashboard: token persistence ----------------
check("dash: saveToken defined with localStorage try/catch",
      re.search(r"function saveToken\(t\)\{[^}]*try \{ localStorage\.setItem", DASH_SRC) is not None)
check("dash: saveToken falls back to sessionStorage",
      "sessionStorage.setItem('df_token', t)" in DASH_SRC)
check("dash: loadToken defined with both stores",
      re.search(r"function loadToken\(\)\{[^}]*localStorage\.getItem\('df_token'\) \|\| sessionStorage\.getItem\('df_token'\)", DASH_SRC) is not None)
check("dash: initial token comes from loadToken()", "let token = loadToken();" in DASH_SRC)
check("dash: no other direct localStorage.getItem('df_token')",
      DASH_SRC.count("localStorage.getItem('df_token')") == 1)
check("dash: only saveToken writes localStorage df_token",
      DASH_SRC.count("localStorage.setItem('df_token'") == 1)
check("dash: logout clears both stores",
      "localStorage.removeItem('df_token')" in DASH_SRC and "sessionStorage.removeItem('df_token')" in DASH_SRC)

# ---------------- dashboard: doLogin error distinction ----------------
# Scoped to doLogin(); message literals are locale-dependent (Spanish since
# 2026-10-09) so the contract asserts branch placement + message semantics,
# not exact English copy. The incident this guards: every failure mode
# rendering as "Wrong password."
_do_login = DASH_SRC[DASH_SRC.index("async function doLogin(){"):
                     DASH_SRC.index("async function updateHostStats(){")]
_conn_msg = re.search(r"\} catch\(e\)\{\s*//[^\n]*\n\s*\$\('err'\)\.textContent = '([^']*)';",
                      _do_login)
_m401 = re.search(r"if \(r\.status === 401 \|\| \(j && !j\.token\)\) \{\s*"
                  r"\$\('err'\)\.textContent = '([^']*)';", _do_login)
_merr = re.search(r"if \(!r\.ok \|\| !j \|\| !j\.token\) \{\s*"
                  r"\$\('err'\)\.textContent = '([^']*)' \+ r\.status", _do_login)
check("dash: doLogin shows connection-failure message (not wrong-password)",
      _conn_msg is not None
      and re.search(r"connection failed|sin conexi.n|check your network|revisa tu red",
                    _conn_msg.group(1), re.I) is not None
      and "wrong password" not in _conn_msg.group(1).lower()
      and "contraseña incorrecta" not in _conn_msg.group(1).lower())
check("dash: doLogin shows wrong-password message only on 401/missing-token branch",
      _m401 is not None
      and re.search(r"wrong password|contraseña incorrecta", _m401.group(1), re.I) is not None)
check("dash: doLogin shows server-error branch with status",
      _merr is not None
      and _conn_msg is not None and _m401 is not None
      and _merr.group(1) != _conn_msg.group(1)
      and _merr.group(1) != _m401.group(1))
check("dash: doLogin stores token via saveToken", "saveToken(j.token);" in DASH_SRC)
check("dash: doLogin no longer blames every exception on password",
      _conn_msg is not None and _m401 is not None
      and _conn_msg.group(1) != _m401.group(1))
check("dash: api() still sends Bearer token",
      "'Authorization':'Bearer '+token" in DASH_SRC)
check("dash: 401 still triggers logout", "if (r.status === 401) { logout();" in DASH_SRC)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
