"""Unit tests for comma-tolerant bulk account import (manager/app.py).

Run: python3 test_bulk_accounts_commas.py
Covers: parse_bulk_accounts password-with-commas contract — commas inside
the password are preserved (previously silently truncated at the first
comma, saving a wrong password and failing every later login). Only a
trailing segment that is exactly an auto literal (1/true/yes/auto/y,
case-insensitive) is stripped as the auto-login flag.

Hermetic: imports the real manager/app.py; no docker, no network,
no filesystem writes.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("bulkcommas_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

def one(line):
    e, errs = app.parse_bulk_accounts(line)
    return (e[0] if e else None), errs

# ---------------- the bug: commas in the password ----------------
e, _ = one("rbx1,user1,pa,ss,word")
check("bug: commas in password preserved",
      e["password"] == "pa,ss,word" and e["auto_login"] is False
      and e["name"] == "rbx1" and e["username"] == "user1")

e, _ = one("rbx1,user1,pa,ss,word,auto")
check("commas in password + trailing auto flag",
      e["password"] == "pa,ss,word" and e["auto_login"] is True)

e, _ = one("rbx1,user1,pa,ss,TRUE")
check("trailing auto literal is case-insensitive",
      e["password"] == "pa,ss" and e["auto_login"] is True)

e, _ = one("rbx1,user1,pa,ss,1")
check("trailing '1' is an auto flag",
      e["password"] == "pa,ss" and e["auto_login"] is True)

e, _ = one("rbx1,user1,pa,ss,no")
check("trailing non-flag segment stays in the password",
      e["password"] == "pa,ss,no" and e["auto_login"] is False)

# ---------------- old shapes unchanged ----------------
e, _ = one("rbx1,user1,pass1")
check("plain 3-field line unchanged",
      e["password"] == "pass1" and e["auto_login"] is False)

e, _ = one("rbx2, user2 , pass2 , auto")
check("4-field line with whitespace unchanged",
      e["username"] == "user2" and e["password"] == "pass2"
      and e["auto_login"] is True)

e, _ = one("rbx1,u,p,YES")
check("YES still an auto flag", e["password"] == "p"
      and e["auto_login"] is True)

e, _ = one("rbx1,u,p")
check("no 4th field -> not auto", e["password"] == "p"
      and e["auto_login"] is False)

# ---------------- edges ----------------
e, _ = one("rbx1,user1,pa ss,wo rd")
check("internal spaces in password preserved",
      e["password"] == "pa ss,wo rd")

e, errs = one("rbx1,user1,,auto")
check("empty password + auto flag -> missing password",
      e is None and len(errs) == 1
      and errs[0]["error"] == "missing password")

e, errs = one("rbx1,onlyuser")
check("2 fields still an error",
      e is None and errs[0]["error"] == "need name,username,password")

e, errs = one("rbx1,user1,")
check("empty password still an error",
      e is None and errs[0]["error"] == "missing password")

# name/username are the comma anchors: a comma in the username splits it
# (documented — usernames with commas are not supported)
e, _ = one("rbx1,us,er,p")
check("comma in username splits at the anchors (documented)",
      e["username"] == "us" and e["password"] == "er,p")

e, _ = one("rbx1," + "U" * 150 + "," + "P," * 150)
check("username truncated to 100", len(e["username"]) == 100)
check("password with commas truncated to 200",
      len(e["password"]) == 200)

entries, errs = app.parse_bulk_accounts(None)
check("None input -> no entries, no errors", entries == [] and errs == [])

entries, errs = app.parse_bulk_accounts("  rbx1,u,p,q  \n# comment\n\nrbx2,u2,p2")
check("line whitespace + comments + blanks",
      len(entries) == 2 and entries[0]["password"] == "p,q"
      and entries[1]["password"] == "p2" and errs == [])

# ambiguity is documented, not silent: a password literally ending in an
# auto literal loses that segment — set it via the Account panel instead
e, _ = one("rbx1,user1,my,true")
check("documented ambiguity: trailing 'true' is the flag",
      e["password"] == "my" and e["auto_login"] is True)

# ---------------- end-to-end: the saved credential is the typed one ----------------
_SAVED = {}
app.load_instances = lambda: {"rbx1": {}}
app.load_accounts = lambda: {}
def fake_save(data):
    _SAVED.update(data)
app.save_accounts = fake_save

entries, errs = app.parse_bulk_accounts("rbx1,alice,s3c,ret,auto")
saved, failed = app.bulk_save_accounts(entries, app.load_instances())
check("e2e: comma password saved byte-identical",
      saved == ["rbx1"] and failed == []
      and _SAVED["rbx1"]["password"] == "s3c,ret"
      and _SAVED["rbx1"]["auto_login"] is True)

# ---------------- source contracts ----------------
src = open(os.path.join(HERE, "..", "..", "manager", "app.py")).read()
check("src: docstring documents comma rule",
      "Passwords may contain commas" in src)
check("src: no parts[2]-only password",
      'name, username, password = parts[0], parts[1], parts[2]' not in src)

html = open(os.path.join(HERE, "..", "..", "manager", "static",
                         "index.html")).read()
check("dash: comma hint present",
      "La contrase\u00f1a puede contener comas" in html)
check("dash: format hint intact", "name,username,password[,auto]" in html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
