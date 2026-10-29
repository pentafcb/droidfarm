#!/usr/bin/env python3
"""Tests for the node-offline card badge (dead wing node vs stopped instance).

GAP: a remote instance on a wing node that stopped heartbeating contributes
nothing to fetch_remote_statuses(), so its card reads "stopped" — exactly
like an instance the user deliberately stopped. There was no signal on the
card for which one it was; only the Nodes modal's online flag said so, one
modal deep. manager/app.py now emits node_offline per remote instance
(heartbeat stale >120s, same rule as the /api/providers online flag, or the
provider record missing), and the dashboard renders a red "Nodo sin
conexión" badge on the card, outranking the degraded/drift badges (both
come from the last-known-good cache and are stale once the node is gone),
and disables the Ver button (a viewer on a dead node can only fail).

Covers (hermetic):
  A. manager.node_offline() — staleness boundary, missing/garbage records
  B. /api/instances contract — both branches emit node_offline
  C. dashboard contract — badge block, priority order, Ver disabled
  D. behavioral node test of the extracted badge block
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
    print("node-offline-badge tests")
    try:
        app = load_app()
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        app = None

    # ---------- A. node_offline() ----------
    print("== A. node_offline() ==")
    if app is not None:
        now = time.time()
        check("helper defined", callable(getattr(app, "node_offline", None)))
        check("staleness constant is 120s",
              getattr(app, "NODE_OFFLINE_STALE_S", None) == 120)
        check("staleness matches nodes_attention rule",
              getattr(app, "NODE_OFFLINE_STALE_S", None)
              == getattr(app, "NODES_ATTENTION_STALE_S", None))
        check("fresh node -> False",
              app.node_offline({"srt": {"last_seen": now}}, "srt") is False)
        check("119s-old node -> False",
              app.node_offline({"srt": {"last_seen": now - 119}},
                               "srt") is False)
        check("just under 120s old -> False (boundary, epsilon-safe: "
              "an exact-120s probe is racy against the clock inside the "
              "helper, so the boundary is probed 0.01s inside it)",
              app.node_offline({"srt": {"last_seen": time.time() - 119.99}},
                               "srt") is False)
        check("121s-old node -> True",
              app.node_offline({"srt": {"last_seen": now - 121}},
                               "srt") is True)
        check("missing provider id -> True",
              app.node_offline({"srt": {"last_seen": now}},
                               "ghost") is True)
        check("empty providers dict -> True",
              app.node_offline({}, "srt") is True)
        check("providers None -> True (never raises)",
              app.node_offline(None, "srt") is True)
        check("providers non-dict -> True (never raises)",
              app.node_offline(["srt"], "srt") is True)
        check("provider non-dict -> True (never raises)",
              app.node_offline({"srt": "junk"}, "srt") is True)
        check("missing last_seen -> True",
              app.node_offline({"srt": {}}, "srt") is True)
        check("last_seen 0 -> True",
              app.node_offline({"srt": {"last_seen": 0}}, "srt") is True)
        check("garbage last_seen -> True (never raises)",
              app.node_offline({"srt": {"last_seen": "junk"}},
                               "srt") is True)
        check("last_seen None -> True (never raises)",
              app.node_offline({"srt": {"last_seen": None}},
                               "srt") is True)
        check("numeric-string last_seen tolerated -> False",
              app.node_offline({"srt": {"last_seen": str(now)}},
                               "srt") is False)
        check("future last_seen -> False",
              app.node_offline({"srt": {"last_seen": now + 3600}},
                               "srt") is False)
    else:
        bad("A section skipped (no app module)")

    # ---------- B. /api/instances contract ----------
    print("== B. /api/instances contract ==")
    try:
        src = open(APP, encoding="utf-8").read()
    except OSError as e:
        bad("read app.py", str(e))
        src = ""
    check("remote branch emits node_offline via helper",
          '"node_offline": node_offline(providers, prov)' in src)
    check("local branch emits node_offline False",
          '"node_offline": False' in src)
    check("helper defined with (providers, prov) signature",
          re.search(r"def node_offline\(providers, prov\):", src) is not None)
    check("remote emit sits in the remote branch (before local emit)",
          src.find('"node_offline": node_offline(providers, prov)')
          < src.find('"node_offline": False'))
    check("no other node_offline emit (exactly two sites)",
          src.count('"node_offline":') == 2)

    # ---------- C. dashboard contract ----------
    print("== C. dashboard contract ==")
    try:
        html = open(HTML, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        html = ""
    check("badge block has begin/end markers",
          "// == node-offline-badge begin ==" in html
          and "// == node-offline-badge end ==" in html)
    # markers sit indented inside refresh()'s forEach, so the end marker
    # is matched with its leading whitespace tolerated
    m = re.search(
        r"// == node-offline-badge begin ==\n(.*?)"
        r"\n\s*// == node-offline-badge end ==",
        html, re.S)
    check("badge block extractable", m is not None)
    if m:
        block = m.group(1)
        check("badge block has no emojis", EMOJI_RE.search(block) is None)
        check("badge block has no innerHTML", "innerHTML" not in block)
        check("badge text is Spanish, no emojis",
              "Nodo sin conexión" in block)
        check("badge reuses red .setup.degraded class (no new CSS)",
              'class="setup degraded"' in block)
        check("provider name escaped in tooltip",
              "esc(i.provider_name||i.provider||'')" in block)
    check("offline badge outranks degraded in the chain",
          html.find("if (nodeOfflineBadge) setupBadge = nodeOfflineBadge;")
          < html.find("else if (i.degraded) setupBadge"))
    # The Ver-disabled condition gained ||i.node_fetch_degraded when the
    # node-fetch badge landed (t139): the button is disabled for both
    # dead-node and failing-endpoint cards. The assertion checks the
    # contract (node_offline disables Ver), not the old exact string.
    check("Ver button disabled when node offline",
          "(i.setup!=='ready'||i.node_offline||i.node_fetch_degraded)?"
          "' disabled':''" in html)
    check("card still renders degraded badge when node online",
          "else if (i.degraded) setupBadge" in html)

    # ---------- D. behavioral node test of the extracted block ----------
    print("== D. node driver ==")
    if m:
        block = m.group(1)
        # One driver per case: the block is re-run with a fresh `i` each
        # time (no eval tricks — the block declares nodeOfflineBadge once
        # per process, so a fresh process per case is the honest harness).
        cases = [
            ("online-false", "{node_offline: false, provider_name: 'srt'}",
             ["EMPTY"]),
            ("online-undefined", "{provider_name: 'srt'}", ["EMPTY"]),
            ("offline-named",
             "{node_offline: true, provider_name: 'srt'}",
             ["Nodo sin conexión", "srt"]),
            ("offline-id-fallback",
             "{node_offline: true, provider: 'wing-7'}",
             ["Nodo sin conexión", "wing-7"]),
            ("offline-escaped",
             "{node_offline: true, provider_name: 'a<b'}",
             ["Nodo sin conexión", "&lt;"]),
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
                    "console.log(nodeOfflineBadge === '' "
                    "? 'ok EMPTY' : 'FAIL not empty: ' + nodeOfflineBadge);")
            else:
                for w in wants:
                    asserts.append(
                        f"console.log(nodeOfflineBadge.indexOf({w!r}) !== -1 "
                        f"? 'ok has {w}' : 'FAIL missing {w}: ' "
                        "+ nodeOfflineBadge);")
                if cname == "offline-escaped":
                    asserts.append(
                        "console.log(nodeOfflineBadge.indexOf('a<b') === -1 "
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
        bad("D section skipped (block not extractable)")

    print(f"PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
