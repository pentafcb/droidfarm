"""Unit tests for bulk Roblox account import (manager/app.py + dashboard).

Run: python3 test_bulk_accounts.py
Covers: parse_bulk_accounts (format variants, comments/blanks, bad lines,
auto flag, truncation), bulk_save_accounts (unknown-instance refusal,
merging, force_auto), roblox_account_summary (coverage, never passwords),
route placement/auth, and the dashboard bulk-import contract.

Hermetic: load_accounts/save_accounts/load_instances are monkeypatched;
no docker, no network, no filesystem writes.
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

app = load("bulkaccounts_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

# ---------------- fakes ----------------
_FAKE_ACCOUNTS = {}
_SAVED = {"count": 0, "last": None}

def fake_load_accounts():
    return dict(_FAKE_ACCOUNTS)

def fake_save_accounts(data):
    _SAVED["count"] += 1
    _SAVED["last"] = dict(data)
    _FAKE_ACCOUNTS.clear()
    _FAKE_ACCOUNTS.update(data)

_FAKE_INSTANCES = {"rbx1": {}, "rbx2": {}, "srt1": {}}
# login results: deterministic empty (summary adds per-account login fields)
app.load_login_results = lambda: {}
app.load_accounts = fake_load_accounts
app.save_accounts = fake_save_accounts
app.load_instances = lambda: dict(_FAKE_INSTANCES)

def reset():
    _FAKE_ACCOUNTS.clear()
    _SAVED["count"] = 0
    _SAVED["last"] = None

# ---------------- parse_bulk_accounts ----------------
entries, errs = app.parse_bulk_accounts(
    "rbx1,user1,pass1\nrbx2, user2 , pass2 , auto\n\n# comment\nsrt1,u3,p3,YES")
check("parse: 3 entries", len(entries) == 3)
check("parse: no errors", errs == [])
check("parse: fields", entries[0] == {"name": "rbx1", "username": "user1",
      "password": "pass1", "auto_login": False})
check("parse: whitespace stripped + auto flag", entries[1]["username"] == "user2"
      and entries[1]["password"] == "pass2" and entries[1]["auto_login"] is True)
check("parse: YES -> auto", entries[2]["auto_login"] is True)

for val in ("1", "true", "True", "y", "Y", "auto"):
    e, _ = app.parse_bulk_accounts(f"rbx1,u,p,{val}")
    check(f"parse: auto variant {val!r}", e and e[0]["auto_login"] is True)
e, _ = app.parse_bulk_accounts("rbx1,u,p,no")
check("parse: 'no' not auto", e and e[0]["auto_login"] is False)
e, _ = app.parse_bulk_accounts("rbx1,u,p")
check("parse: missing 4th -> not auto", e and e[0]["auto_login"] is False)

_, errs = app.parse_bulk_accounts("rbx1,onlyuser\n,,\nrbx1,,p\nrbx1,u,\n,x,")
check("parse: 5 bad lines flagged", len(errs) == 5)
check("parse: line numbers right", [e["line"] for e in errs] == [1, 2, 3, 4, 5])
check("parse: errors have text",
      all(e["error"] for e in errs))

e, _ = app.parse_bulk_accounts("rbx1," + "U"*150 + "," + "P"*250)
check("parse: username truncated to 100", len(e[0]["username"]) == 100)
check("parse: password truncated to 200", len(e[0]["password"]) == 200)

entries, errs = app.parse_bulk_accounts("")
check("parse: empty input -> nothing", entries == [] and errs == [])
entries, errs = app.parse_bulk_accounts("# only comments\n\n")
check("parse: comments-only -> nothing", entries == [] and errs == [])

# commas inside the password are preserved (bulk-accounts-commas package):
# only a trailing auto literal is stripped, never password content
e, _ = app.parse_bulk_accounts("rbx1,u,pass,with,commas")
check("parse: commas kept in password, not auto",
      e[0]["password"] == "pass,with,commas" and e[0]["auto_login"] is False)

# ---------------- bulk_save_accounts ----------------
reset()
entries, _ = app.parse_bulk_accounts("rbx1,alice,s3cr3t,auto\nnope,bob,x\nrbx2,carol,y")
saved, failed = app.bulk_save_accounts(entries, app.load_instances())
check("save: known instances saved", saved == ["rbx1", "rbx2"])
check("save: unknown instance refused",
      len(failed) == 1 and failed[0]["name"] == "nope"
      and failed[0]["error"] == "unknown instance")
check("save: persisted once", _SAVED["count"] == 1)
check("save: rbx1 entry", _SAVED["last"]["rbx1"]["username"] == "alice"
      and _SAVED["last"]["rbx1"]["password"] == "s3cr3t"
      and _SAVED["last"]["rbx1"]["auto_login"] is True)
check("save: rbx2 no auto", _SAVED["last"]["rbx2"]["auto_login"] is False)

# merge: pre-existing account for another instance is preserved
reset()
_FAKE_ACCOUNTS["srt1"] = {"username": "old", "password": "oldpw", "auto_login": False}
entries, _ = app.parse_bulk_accounts("rbx1,alice,s3cr3t")
saved, failed = app.bulk_save_accounts(entries, app.load_instances())
check("save: merges, keeps srt1",
      _SAVED["last"]["srt1"]["username"] == "old"
      and _SAVED["last"]["rbx1"]["username"] == "alice")

# force_auto overrides per-entry flags
reset()
entries, _ = app.parse_bulk_accounts("rbx1,alice,s3cr3t")
saved, _ = app.bulk_save_accounts(entries, app.load_instances(), force_auto=True)
check("save: force_auto=True overrides",
      _SAVED["last"]["rbx1"]["auto_login"] is True)

# all-unknown -> nothing saved, no save_accounts call
reset()
entries, _ = app.parse_bulk_accounts("ghost,x,y")
saved, failed = app.bulk_save_accounts(entries, app.load_instances())
check("save: all unknown -> saved empty", saved == [])
check("save: no write on zero saves", _SAVED["count"] == 0)

# ---------------- roblox_account_summary ----------------
reset()
_FAKE_ACCOUNTS.update({
    "rbx1": {"username": "alice", "password": "pw1", "auto_login": True},
    "rbx2": {"username": "carol"},          # no password -> not covered
})
s = app.roblox_account_summary()
check("summary: total", s["total"] == 3)
check("summary: covered counts only full creds", s["covered"] == 1)
check("summary: rbx1 row", s["accounts"]["rbx1"] == {"username": "alice",
      "has_password": True, "auto_login": True,
      "login_ok": None, "login_ts": None, "login_by": None,
      "login_reason": None, "login_attempts": 0})
check("summary: rbx2 has_password False", s["accounts"]["rbx2"]["has_password"] is False)
check("summary: missing instance -> empty row",
      s["accounts"]["srt1"] == {"username": "", "has_password": False,
                                "auto_login": False,
                                "login_ok": None, "login_ts": None, "login_by": None,
                                "login_reason": None, "login_attempts": 0})
check("summary: logged_in key present", s.get("logged_in") == 0)
check("summary: key named password never emitted",
      not re.search(r'"password"\s*:', __import__("json").dumps(s)))
dumped = str(s)
check("summary: no raw password in payload",
      "pw1" not in dumped)

# ---------------- route placement / auth ----------------
src = open(os.path.join(HERE, "..", "..", "manager", "app.py")).read()
do_get = src[src.index("def do_GET"):src.index("def do_POST")]
do_post = src[src.index("def do_POST"):]
check("route: GET /api/roblox-accounts in do_GET",
      'if path == "/api/roblox-accounts":' in do_get)
check("route: GET auth-gated", '"/api/roblox-accounts"' in do_get
      and "_authed()" in do_get)
check("route: POST /api/roblox-accounts/bulk in do_POST",
      'if path == "/api/roblox-accounts/bulk":' in do_post)
# do_POST has a single global auth gate after the public routes
check("route: POST under global auth gate",
      do_post.index('if not self._authed():') <
      do_post.index('if path == "/api/roblox-accounts/bulk":'))
check("route: bulk uses _lock for save", "_lock:" in do_post[
      do_post.index('if path == "/api/roblox-accounts/bulk":'):
      do_post.index('if path == "/api/roblox-accounts/bulk":') + 4000])
check("route: bulk refuses non-list accounts",
      '"accounts must be a list"' in src)

# ---------------- dashboard contract ----------------
html = open(os.path.join(HERE, "..", "..", "manager", "static", "index.html")).read()
for el in ["btnAccounts", "acctBulkModal", "acctBulkText", "acctBulkAuto",
           "btnDoAcctBulk", "btnCancelAcctBulk", "acctBulkRes", "acctCov",
           "acctBulkCov"]:
    check(f"dash: element #{el}", f'id="{el}"' in html)
check("dash: POSTs to bulk endpoint",
      "'/api/roblox-accounts/bulk'" in html)
check("dash: GETs coverage summary", "'/api/roblox-accounts'" in html)
check("dash: format hint", "name,username,password" in html)
check("dash: updateAcctCov defined and wired",
      "function updateAcctCov" in html and "updateAcctCov();" in html)
check("dash: password fields use password type", 'type="password"' in html)
check("dash: results escaped", "r.saved.map(esc)" in html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
