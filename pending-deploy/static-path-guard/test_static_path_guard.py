#!/usr/bin/env python3
"""Tests for static-path-guard (manager only, no agent change).

GAP (security, not a user report): _serve_static is reachable from an
UNAUTHENTICATED GET (do_GET serves "/" and "/static/*" with no auth
check — the dashboard page itself), and its containment check was a bare
prefix test:

    fpath.startswith(STATIC_DIR)

os.path.normpath resolves ".." before the check, so a request for
"/static/../static-evil/secret.txt" resolved to "<dir>/static-evil/
secret.txt" — which STARTS WITH "<dir>/static" — and was served with
HTTP 200. Only a sibling DIRECTORY sharing the "static" prefix was
exposed this way: a sibling file ("/static/../app.py",
"/static/../instances.json") never matched the prefix and stayed
blocked, and URL-encoded traversal ("%2e%2e") is never decoded by
do_GET's urlparse path, so it stayed a literal directory name. Still,
the check was wrong, and any future static*-prefixed sibling directory
(e.g. a staging copy) would have leaked through it unauthenticated.

FIX (manager/app.py only): containment is now a whole-directory match
— fpath == STATIC_DIR or fpath.startswith(STATIC_DIR + os.sep).
No dashboard, agent, or wire-protocol change; legitimate in-tree
traversal ("/static/sub/../index.html") still resolves inside the
tree and still serves.

Covers:
  A. regression proof (the OLD expression allows the sibling escape)
  B. real _serve_static on a fake tree (stubbed _send/_json, patched
     STATIC_DIR): legit serves, sibling escapes 404, outside-tree 404,
     missing 404, encoded traversal 404, sibling file 404
  C. source contracts (fixed expression present, bare expression gone)
  D. snapshot parity (remote-autologin app.py == manager/app.py)
"""
import importlib.util
import os
import sys
import tempfile

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


app = load("spg_app", APP)
SRC = open(APP, encoding="utf-8").read()

# --- A. regression proof: the OLD expression on the escape path --------
with tempfile.TemporaryDirectory() as td:
    static_dir = os.path.join(td, "manager", "static")
    os.makedirs(static_dir)
    evil = os.path.join(td, "manager", "static-evil", "secret.txt")
    os.makedirs(os.path.dirname(evil))
    with open(evil, "w") as f:
        f.write("SECRET")
    seg = "../static-evil/secret.txt"
    old_fpath = os.path.normpath(os.path.join(static_dir, seg))
    check("A1 old bare-prefix check ALLOWS the sibling escape",
          old_fpath.startswith(static_dir) and os.path.isfile(old_fpath),
          old_fpath)
    check("A2 new whole-directory check BLOCKS the sibling escape",
          not (old_fpath == static_dir
               or old_fpath.startswith(static_dir + os.sep)),
          old_fpath)
    # A sibling FILE was never reachable under either check.
    sib_file = os.path.normpath(os.path.join(static_dir, "../app.py"))
    check("A3 sibling file never matched the old prefix either",
          not sib_file.startswith(static_dir), sib_file)

# --- B. real _serve_static on a fake tree -------------------------------
with tempfile.TemporaryDirectory() as td:
    mgr = os.path.join(td, "manager")
    static_dir = os.path.join(mgr, "static")
    sub = os.path.join(static_dir, "sub")
    os.makedirs(sub)
    files = {
        os.path.join(static_dir, "index.html"): b"INDEX",
        os.path.join(static_dir, "app.js"): b"JS",
        os.path.join(sub, "deep.html"): b"DEEP",
    }
    for p, data in files.items():
        with open(p, "wb") as f:
            f.write(data)
    for dirname in ("static-evil", "static-backup"):
        p = os.path.join(mgr, dirname, "secret.txt")
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(b"SECRET")
    with open(os.path.join(mgr, "other.txt"), "wb") as f:
        f.write(b"OTHER")
    with open(os.path.join(td, "outside.txt"), "wb") as f:
        f.write(b"OUTSIDE")

    orig_static = app.STATIC_DIR
    app.STATIC_DIR = static_dir
    try:
        def serve(path):
            h = object.__new__(app.Handler)
            got = {}

            def _send(code, body=b"", ctype="application/json", extra=None):
                got.update(code=code, body=body, ctype=ctype)

            def _json(code, obj):
                got.update(code=code, body=obj, ctype="application/json")

            h._send = _send
            h._json = _json
            h._serve_static(path)
            return got

        r = serve("/")
        check("B1 / serves index.html", r.get("code") == 200
              and r.get("body") == b"INDEX", str(r))
        r = serve("/static/index.html")
        check("B2 /static/index.html serves", r.get("code") == 200
              and r.get("body") == b"INDEX", str(r))
        r = serve("/static/app.js")
        check("B3 js content type", r.get("code") == 200
              and r.get("ctype") == "application/javascript", str(r))
        r = serve("/static/sub/deep.html")
        check("B4 nested file serves", r.get("code") == 200
              and r.get("body") == b"DEEP", str(r))
        r = serve("/static/sub/../index.html")
        check("B5 legit in-tree dotdot still serves", r.get("code") == 200
              and r.get("body") == b"INDEX", str(r))
        r = serve("/static/../static-evil/secret.txt")
        check("B6 sibling static-evil escape -> 404", r.get("code") == 404
              and r.get("body") != b"SECRET", str(r))
        r = serve("/static/../static-backup/secret.txt")
        check("B7 sibling static-backup escape -> 404", r.get("code") == 404
              and r.get("body") != b"SECRET", str(r))
        r = serve("/static/../../outside.txt")
        check("B8 above-tree escape -> 404", r.get("code") == 404
              and r.get("body") != b"OUTSIDE", str(r))
        r = serve("/static/../other.txt")
        check("B9 sibling file -> 404", r.get("code") == 404
              and r.get("body") != b"OTHER", str(r))
        r = serve("/static/missing.html")
        check("B10 missing file -> 404", r.get("code") == 404, str(r))
        r = serve("/static/%2e%2e/static-evil/secret.txt")
        check("B11 encoded traversal stays literal -> 404",
              r.get("code") == 404 and r.get("body") != b"SECRET", str(r))
        r = serve("/static//index.html")
        check("B12 absolute segment cannot escape (404, safe)",
              r.get("code") == 404, str(r))
    finally:
        app.STATIC_DIR = orig_static

# --- C. source contracts ------------------------------------------------
check("C1 fixed whole-directory check present",
      "fpath.startswith(STATIC_DIR + os.sep)" in SRC)
check("C2 bare startswith(STATIC_DIR) check gone",
      "if not fpath.startswith(STATIC_DIR)" not in SRC)
check("C3 _serve_static still serves only files (isfile kept)",
      "or not os.path.isfile(fpath)" in SRC)
check("C4 static routes still public via _serve_static only",
      'if path == "/" or path.startswith("/static/"):' in SRC
      and "self._serve_static(path)" in SRC)

# --- D. snapshot parity --------------------------------------------------
check("D1 remote-autologin app.py snapshot == manager/app.py",
      os.path.isfile(SNAP_APP)
      and open(SNAP_APP, "rb").read() == open(APP, "rb").read())

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
