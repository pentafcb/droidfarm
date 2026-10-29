#!/usr/bin/env python3
"""Tests for the node-fetch-failure card badge (heartbeating node whose
status endpoint keeps failing vs a deliberately stopped instance).

GAP: a wing node that keeps heartbeating (so node_offline() is False) but
whose /api/agent/instances endpoint keeps failing contributes nothing to
fetch_remote_statuses() once the 30s last-known-good cache expires, so its
cards read "stopped" — exactly like instances the user deliberately
stopped — with no badge at all. node_offline-badge explicitly left this
case out ("transient by construction"), but persistence was unmeasured
and unsurfaced: a wedged agent HTTP server misleads for hours.

manager/app.py now counts consecutive failed status fetches per provider
in fetch_remote_statuses() (success resets, stale-heartbeat providers are
never attempted or counted, counters for deleted providers are pruned)
and emits node_fetch_degraded + node_fetch_failures per remote instance
once REMOTE_FETCH_DEGRADED_AFTER (6) consecutive polls fail. The
dashboard renders a red "Nodo sin respuesta" badge on the card, ranking
below node_offline (a dead node outranks a failing endpoint) and above
degraded/drift (both come from the last-known-good cache and are stale
once fetches fail), and disables the Ver button (a viewer on a node
whose agent endpoint fails can only fail in the common wedged-agent
case).

Covers (hermetic):
  A. counter behavior — real fetch_remote_statuses() with a fake
     wing_request (threshold, reset, exception path, stale skip, prune,
     cache reuse preserved, non-dict resp counts)
  B. remote_fetch_degraded() / remote_fetch_failures() — boundary +
     never-raises cases
  C. /api/instances contract — both branches emit both fields
  D. dashboard contract — badge block, priority order, Ver disabled
  E. behavioral node test of the extracted badge block
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
HTML = os.path.join(REPO, "manager", "static", "index.html")

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


EMOJI_RE = re.compile(
    "[\\U0001F000-\\U0001FAFF\\u2600-\\u26FF\\u2700-\\u27BF\\u2B00-\\u2BFF\\uFE0F]")


def load_app():
    spec = importlib.util.spec_from_file_location("df_app", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    print("node-fetch-badge tests")
    try:
        app = load_app()
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        app = None

    # ---------- A. counter behavior (real fetch_remote_statuses) ----------
    print("== A. fetch counter ===")
    if app is not None:
        check("threshold constant is 6",
              getattr(app, "REMOTE_FETCH_DEGRADED_AFTER", None) == 6)
        check("helpers defined",
              callable(getattr(app, "remote_fetch_degraded", None))
              and callable(getattr(app, "remote_fetch_failures", None)))
        now = time.time()
        calls = {"n": 0}
        mode = {"kind": "fail"}

        def fake_wing(_p, _m, _path, **_kw):
            calls["n"] += 1
            if mode["kind"] == "fail":
                return 500, {}
            if mode["kind"] == "raise":
                raise ConnectionError("boom")
            if mode["kind"] == "nondict":
                return 200, [1, 2]
            return 200, {"instances": [{"name": "srt1", "running": True}]}

        orig = app.wing_request
        app.wing_request = fake_wing
        try:
            prov = lambda: {"srt": {"last_seen": time.time()}}  # noqa: E731
            app._remote_fetch_failures.clear()
            app._remote_status_cache.clear()
            check("fresh node: failures 0, not degraded",
                  app.remote_fetch_failures("srt") == 0
                  and app.remote_fetch_degraded(prov(), "srt") is False)
            for _ in range(5):
                app.fetch_remote_statuses(prov())
            check("5 failures: count 5, still not degraded",
                  app.remote_fetch_failures("srt") == 5
                  and app.remote_fetch_degraded(prov(), "srt") is False)
            app.fetch_remote_statuses(prov())
            check("6th failure: count 6, degraded",
                  app.remote_fetch_failures("srt") == 6
                  and app.remote_fetch_degraded(prov(), "srt") is True)
            mode["kind"] = "ok"
            out = app.fetch_remote_statuses(prov())
            check("success resets counter and serves instances",
                  app.remote_fetch_failures("srt") == 0
                  and app.remote_fetch_degraded(prov(), "srt") is False
                  and ("srt", "srt1") in out)
            mode["kind"] = "fail"
            out = app.fetch_remote_statuses(prov())
            check("failure after success: count 1, cache still served",
                  app.remote_fetch_failures("srt") == 1
                  and ("srt", "srt1") in out)
            mode["kind"] = "raise"
            before = calls["n"]
            app.fetch_remote_statuses(prov())
            check("wing_request raising counts as a failure",
                  app.remote_fetch_failures("srt") == 2
                  and calls["n"] == before + 1)
            mode["kind"] = "nondict"
            app.fetch_remote_statuses(prov())
            check("200 with non-dict body counts as a failure",
                  app.remote_fetch_failures("srt") == 3)
            mode["kind"] = "fail"
            stale = {"srt": {"last_seen": time.time() - 121}}
            before = calls["n"]
            out = app.fetch_remote_statuses(stale)
            check("stale-heartbeat provider never attempted, count kept",
                  calls["n"] == before
                  and app.remote_fetch_failures("srt") == 3
                  and out == {})
            check("stale-heartbeat provider is node_offline territory, "
                  "not fetch-degraded",
                  app.remote_fetch_degraded(stale, "srt") is False)
            app._remote_fetch_failures["ghost"] = 9
            app.fetch_remote_statuses(prov())
            check("counter for a deleted provider is pruned",
                  "ghost" not in app._remote_fetch_failures)
            app.fetch_remote_statuses({"srt": "junk"})
            check("non-dict provider entry skipped, not counted",
                  app.remote_fetch_failures("srt") == 4)
        finally:
            app.wing_request = orig
    else:
        bad("A section skipped (no app module)")

    # ---------- B. helper boundary + never-raises ----------
    print("== B. helper cases ==")
    if app is not None:
        now = time.time()
        app._remote_fetch_failures.clear()
        check("missing provider id -> False (never raises)",
              app.remote_fetch_degraded({"srt": {"last_seen": now}},
                                        "ghost") is False)
        check("empty providers dict -> False",
              app.remote_fetch_degraded({}, "srt") is False)
        check("providers None -> False (never raises)",
              app.remote_fetch_degraded(None, "srt") is False)
        check("providers non-dict -> False (never raises)",
              app.remote_fetch_degraded(["srt"], "srt") is False)
        check("provider non-dict -> False (never raises)",
              app.remote_fetch_degraded({"srt": "junk"}, "srt") is False)
        check("missing last_seen -> False (stale by default)",
              app.remote_fetch_degraded({"srt": {}}, "srt") is False)
        check("garbage last_seen -> False (never raises)",
              app.remote_fetch_degraded({"srt": {"last_seen": "junk"}},
                                        "srt") is False)
        check("numeric-string last_seen tolerated, count 0 -> False",
              app.remote_fetch_degraded({"srt": {"last_seen": str(now)}},
                                        "srt") is False)
        app._remote_fetch_failures["srt"] = 6
        check("count at threshold + fresh heartbeat -> True",
              app.remote_fetch_degraded({"srt": {"last_seen": now}},
                                        "srt") is True)
        check("count at threshold + stale heartbeat -> False "
              "(node_offline owns it)",
              app.remote_fetch_degraded(
                  {"srt": {"last_seen": now - 121}}, "srt") is False)
        check("failures helper returns count",
              app.remote_fetch_failures("srt") == 6)
        check("failures helper unknown provider -> 0",
              app.remote_fetch_failures("ghost") == 0)
        check("failures helper non-string prov -> 0 (never raises)",
              app.remote_fetch_failures(None) == 0)
        app._remote_fetch_failures.clear()

    # ---------- C. /api/instances contract ----------
    print("== C. /api/instances contract ==")
    try:
        src = open(APP, encoding="utf-8").read()
    except OSError as e:
        bad("read app.py", str(e))
        src = ""
    check("remote branch emits node_fetch_degraded via helper",
          '"node_fetch_degraded": remote_fetch_degraded(' in src)
    check("remote branch emits node_fetch_failures via helper",
          '"node_fetch_failures": remote_fetch_failures(prov)' in src)
    check("local branch emits node_fetch_degraded False",
          '"node_fetch_degraded": False' in src)
    check("local branch emits node_fetch_failures 0",
          '"node_fetch_failures": 0' in src)
    check("remote emit sits in the remote branch (before local emit)",
          src.find('"node_fetch_degraded": remote_fetch_degraded(')
          < src.find('"node_fetch_degraded": False'))
    check("no other node_fetch_degraded emit (exactly two sites)",
          src.count('"node_fetch_degraded":') == 2)
    check("no other node_fetch_failures emit (exactly two sites)",
          src.count('"node_fetch_failures":') == 2)
    check("counter reset on success, increment on failure in fetcher",
          "_remote_fetch_failures[pid] = 0" in src
          and src.count("_remote_fetch_failures.get(pid, 0) + 1") == 2)

    # ---------- D. dashboard contract ----------
    print("== D. dashboard contract ==")
    try:
        html = open(HTML, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        html = ""
    check("badge block has begin/end markers",
          "// == node-fetch-badge begin ==" in html
          and "// == node-fetch-badge end ==" in html)
    m = re.search(
        r"// == node-fetch-badge begin ==\n(.*?)"
        r"\n\s*// == node-fetch-badge end ==",
        html, re.S)
    check("badge block extractable", m is not None)
    if m:
        block = m.group(1)
        check("badge block has no emojis", EMOJI_RE.search(block) is None)
        check("badge block has no innerHTML", "innerHTML" not in block)
        check("badge text is Spanish, no emojis",
              "Nodo sin respuesta" in block)
        check("badge reuses red .setup.degraded class (no new CSS)",
              'class="setup degraded"' in block)
        check("badge guarded by !node_offline (offline outranks)",
              "!i.node_offline && i.node_fetch_degraded" in block)
        check("provider name escaped in tooltip",
              "esc(i.provider_name||i.provider||'')" in block)
        check("failure count interpolated in tooltip",
              "i.node_fetch_failures" in block)
    check("offline badge outranks fetch badge in the chain",
          html.find("if (nodeOfflineBadge) setupBadge = nodeOfflineBadge;")
          < html.find("else if (nodeFetchBadge) setupBadge = nodeFetchBadge;"))
    check("fetch badge outranks degraded in the chain",
          html.find("else if (nodeFetchBadge) setupBadge = nodeFetchBadge;")
          < html.find("else if (i.degraded) setupBadge"))
    check("Ver button disabled when fetch degraded",
          "(i.setup!=='ready'||i.node_offline||i.node_fetch_degraded)?"
          "' disabled':''" in html)
    check("no auto-refresh change (mirrors node_offline precedent: "
          "fetch-degraded does not force the 5s refresh loop)",
          "node_fetch_degraded" not in
          html[html.find("Auto-refresh while"):html.find("Auto-refresh while") + 300]
          if "Auto-refresh while" in html else False)

    # ---------- E. node driver ----------
    print("== E. node driver ==")
    if m:
        block = m.group(1)
        cases = [
            ("healthy",
             "{node_offline: false, node_fetch_degraded: false, "
             "provider_name: 'srt'}",
             ["EMPTY"]),
            ("degraded-undefined",
             "{node_offline: false, provider_name: 'srt'}",
             ["EMPTY"]),
            ("fetch-degraded",
             "{node_offline: false, node_fetch_degraded: true, "
             "node_fetch_failures: 7, provider_name: 'srt'}",
             ["Nodo sin respuesta", "srt", "7 intentos"]),
            ("offline-outranks",
             "{node_offline: true, node_fetch_degraded: true, "
             "node_fetch_failures: 9, provider_name: 'srt'}",
             ["EMPTY"]),
            ("fetch-escaped",
             "{node_offline: false, node_fetch_degraded: true, "
             "node_fetch_failures: 6, provider_name: 'a<b'}",
             ["Nodo sin respuesta", "&lt;"]),
        ]
        all_ok = True
        for cname, i_src, wants in cases:
            stub = (
                "function esc(s){ return String(s).replace(/&/g,'&amp;')"
                ".replace(/</g,'&lt;').replace(/>/g,'&gt;')"
                ".replace(/\"/g,'&quot;'); }\n"
                f"var i = {i_src};\n"
            )
            asserts = []
            if wants == ["EMPTY"]:
                asserts.append(
                    "console.log(nodeFetchBadge === '' "
                    "? 'ok EMPTY' : 'FAIL not empty: ' + nodeFetchBadge);")
            else:
                for w in wants:
                    asserts.append(
                        f"console.log(nodeFetchBadge.indexOf({w!r}) !== -1 "
                        f"? 'ok has {w}' : 'FAIL missing {w}: ' "
                        "+ nodeFetchBadge);")
                if cname == "fetch-escaped":
                    asserts.append(
                        "console.log(nodeFetchBadge.indexOf('a<b') === -1 "
                        "? 'ok no raw html' : 'FAIL raw html leaked');")
            with tempfile.NamedTemporaryFile("w", suffix=".js",
                                             delete=False) as f:
                f.write(stub + block + "\n" + "\n".join(asserts) + "\n")
                path = f.name
            try:
                p = subprocess.run(["node", path], capture_output=True,
                                   text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as e:
                bad(f"node driver run ({cname})", str(e))
                all_ok = False
                p = None
            finally:
                os.unlink(path)
            if p is not None:
                check(f"node driver exited 0 ({cname})",
                      p.returncode == 0, p.stderr[:200])
                for line in (p.stdout or "").splitlines():
                    if line.startswith("ok "):
                        ok(f"node {cname}: " + line[3:])
                    elif line.startswith("FAIL "):
                        bad(f"node {cname}: " + line[5:])
                        all_ok = False
        check("all node driver cases ran", all_ok)
    else:
        bad("E section skipped (block not extractable)")

    print(f"PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
