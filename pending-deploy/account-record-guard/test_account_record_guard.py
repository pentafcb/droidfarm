#!/usr/bin/env python3
"""Tests for account-record-guard (manager only, no agent change).

GAP (the account-side twin of t155, flagged as the next audit
candidate in the t155 entry): t154/t155 fixed provider and instance
records, but an ACCOUNT record inside a valid roblox_accounts.json
dict can still be the wrong shape — a hand edit or partial write can
leave a string record. Every caller that trusted the record shape
then crashed the handler thread with no response:

  1. bulk_save_accounts: `acct["username"] = ...` raised TypeError on
     a string record (bulk import dropped mid-save).
  2. roblox_account_summary: `acct.get("username")` raised
     AttributeError on a string record and killed the WHOLE fleet
     coverage poll (GET /api/roblox-accounts).
  3. _roblox_login_inner: a NON-EMPTY string record is truthy, so the
     old `not acct` guard did not save it — `acct.get("username")`
     raised AttributeError and the login request dropped.
  4. GET /api/instances/<n>/roblox-account: `acct.get` raised
     AttributeError on a string record.
  5. POST /api/instances/<n>/roblox-account: item assignment raised
     TypeError on a string record (Account panel save dropped).

FIX (manager/app.py only): account_of(record) — a dict record passes
through, a non-dict record degrades to {} (no username, no
password). Read paths report "no account"; write paths replace the
corrupt record with the new fields (it held no credentials to
preserve). Valid records behave identically; no dashboard, agent, or
wire-protocol change. The auto-login watcher already guarded with
isinstance(acct, dict) and is untouched.

Covers:
  A. regression proof (old idioms necessarily crashed on these shapes)
  B. helper account_of matrix
  C. real bulk_save_accounts (stubbed load/save accounts)
  D. real roblox_account_summary (stubbed loads)
  E. real _roblox_login_inner early return (stubbed load_accounts)
  F. source contracts (all 5 sites use account_of; watcher guard kept)
  G. snapshot parity (remote-autologin app.py == manager/app.py)
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
SNAP_APP = os.path.join(REPO, "pending-deploy", "remote-autologin", "app.py")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def check(name, cond, detail=""):
    if cond:
        ok(name)
    else:
        bad(name, detail)


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("arg_app", APP)
app_src = open(APP, encoding="utf-8").read()

print("== A. regression proof ==")
try:
    "oops".get("username")
    bad("str record .get raises AttributeError", "no exception")
except AttributeError:
    ok("str record .get raises AttributeError")
try:
    r = "oops"
    r["username"] = "u"
    bad("str record item assignment raises TypeError", "no exception")
except TypeError:
    ok("str record item assignment raises TypeError")
try:
    acct = "oops"
    _ = (not acct or not acct.get("username"))
    bad("truthy str record slips past `not acct` guard", "no exception")
except AttributeError:
    ok("truthy str record slips past `not acct` guard")

print("== B. helper account_of ==")
check("dict passes through (same object)",
      app.account_of({"username": "u"}) == {"username": "u"})
check("empty dict passes through", app.account_of({}) == {})
check("None -> {}", app.account_of(None) == {})
check("string -> {}", app.account_of("oops") == {})
check("list -> {}", app.account_of(["u", "p"]) == {})
check("int -> {}", app.account_of(7) == {})
check("bool -> {}", app.account_of(True) == {})
check("account_of never raises on junk",
      app.account_of(object()) == {})

print("== C. real bulk_save_accounts ==")
saved_accounts = {}


def _load_acct():
    return dict(saved_accounts)


def _save_acct(data):
    saved_accounts.clear()
    saved_accounts.update(data)


app.load_accounts = _load_acct
app.save_accounts = _save_acct
saved_accounts.update({
    "rbx1": "corrupt-string",
    "rbx2": {"username": "old", "password": "oldpw",
             "auto_login": False, "login_boot": 123},
    "rbx3": "untouched-corrupt",
})
saved, failed = app.bulk_save_accounts(
    [{"name": "rbx1", "username": "new", "password": "newpw",
      "auto_login": True},
     {"name": "rbx2", "username": "u2", "password": "p2",
      "auto_login": False},
     {"name": "ghost", "username": "g", "password": "gp",
      "auto_login": False}],
    {"rbx1": {}, "rbx2": {}, "rbx3": {}})
check("bulk: both valid names saved", saved == ["rbx1", "rbx2"],
      f"saved={saved}")
check("bulk: unknown instance refused",
      failed == [{"name": "ghost", "error": "unknown instance"}],
      f"failed={failed}")
check("bulk: corrupt record replaced by new entry",
      saved_accounts["rbx1"] == {"username": "new", "password": "newpw",
                                  "auto_login": True},
      f"rbx1={saved_accounts.get('rbx1')}")
check("bulk: valid record merged, extra keys preserved",
      saved_accounts["rbx2"] == {"username": "u2", "password": "p2",
                                  "auto_login": False, "login_boot": 123},
      f"rbx2={saved_accounts.get('rbx2')}")
check("bulk: untouched corrupt record left alone",
      saved_accounts["rbx3"] == "untouched-corrupt")

print("== D. real roblox_account_summary ==")
app.load_instances = lambda: {"rbx1": {}, "rbx2": {}, "rbx3": {}}
app.load_accounts = lambda: {
    "rbx1": "corrupt-string",
    "rbx2": {"username": "u2", "password": "p2", "auto_login": True},
}
app.load_login_results = lambda: {
    "rbx2": {"ok": True, "ts": 100, "by": "manual", "reason": "",
             "attempts": 1},
}
summ = app.roblox_account_summary()
check("summary: corrupt record reads as no account",
      summ["accounts"]["rbx1"] == {"username": "", "has_password": False,
                                    "auto_login": False, "login_ok": None,
                                    "login_ts": None, "login_by": None,
                                    "login_reason": None,
                                    "login_attempts": 0},
      f"rbx1={summ['accounts'].get('rbx1')}")
check("summary: valid record normal",
      summ["accounts"]["rbx2"]["username"] == "u2"
      and summ["accounts"]["rbx2"]["has_password"] is True
      and summ["accounts"]["rbx2"]["login_ok"] is True)
check("summary: missing record reads as no account",
      summ["accounts"]["rbx3"]["username"] == "")
check("summary: coverage counts only valid accounts",
      (summ["covered"], summ["logged_in"], summ["total"]) == (1, 1, 3),
      f"{summ['covered']}/{summ['logged_in']}/{summ['total']}")
check("summary: no password value anywhere",
      "p2" not in repr(summ))

print("== E. real _roblox_login_inner early return ==")
for label, record in (("corrupt string", "oops"),
                      ("corrupt list", ["u"]),
                      ("missing record", None),
                      ("empty dict", {}),
                      ("username only", {"username": "u"})):
    app.load_accounts = (lambda r: (lambda: {"rbx1": r}))(record)
    try:
        ok_flag, msg = app._roblox_login_inner("rbx1")
        check(f"login inner: {label} -> clean no-credentials refusal",
              ok_flag is False and msg == "no credentials saved for this instance",
              f"got {(ok_flag, msg)}")
    except Exception as exc:  # noqa: BLE001 - test must report, not raise
        bad(f"login inner: {label} -> clean no-credentials refusal",
            f"raised {type(exc).__name__}: {exc}")

print("== F. source contracts ==")


def fn_src(name):
    start = app_src.index(f"def {name}(")
    try:
        end = app_src.index("\ndef ", start + 1)
    except ValueError:
        end = len(app_src)
    return app_src[start:end]


check("bulk_save_accounts uses account_of",
      "account_of(accounts.get(" in fn_src("bulk_save_accounts"))
check("roblox_account_summary uses account_of",
      "account_of(accounts.get(" in fn_src("roblox_account_summary"))
check("_roblox_login_inner uses account_of",
      "account_of(accounts.get(" in fn_src("_roblox_login_inner"))
check("GET roblox-account route uses account_of",
      "acct = account_of(accounts.get(name))" in app_src)
check("POST roblox-account route uses account_of",
      app_src.count("acct = account_of(accounts.get(name))") >= 2)
check("no unguarded accounts.get(name, {}) remains outside docstring",
      app_src.replace(fn_src("account_of"), "").count(
          "accounts.get(name, {})") == 0)
check("watcher isinstance guard kept",
      'if not isinstance(acct, dict) or not acct.get("auto_login"):'
      in app_src)

print("== G. snapshot parity ==")
check("remote-autologin app.py snapshot == manager/app.py",
      open(SNAP_APP, encoding="utf-8").read() == app_src)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
