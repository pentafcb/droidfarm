#!/usr/bin/env python3
"""Tests for the Nodes-button attention badge (remote-node degraded -> topbar).

GAP: remote-node selfwatch DEGRADED maps already surface inside the Nodes
modal (per-node "needs attention" badge), but the topbar Nodos button
itself gave no signal — a degraded wing node was invisible until Penta
opened the modal. manager/app.py now reads the live providers' degraded
maps into GET /api/instances as nodes_attention, and the dashboard
renders a red count badge on the Nodos button on every grid refresh.

Covers (hermetic):
  A. manager.nodes_attention_summary() — provider shapes, staleness, safety
  B. /api/instances contract — nodes_attention wired into the response
  C. agent cross-check — selfwatch_state() really builds the degraded map
     the manager consumes from heartbeats
  D. dashboard contract + behavioral node test of the extracted badge block
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
AGENT = os.path.join(REPO, "wing", "agent.py")

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


def prov(last_seen, degraded, name=None):
    p = {"last_seen": last_seen, "selfwatch": {"recoveries": 0,
                                               "degraded": degraded}}
    if name is not None:
        p["name"] = name
    return p


def main():
    print("nodes-attention-badge tests")
    try:
        app = load_app()
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        app = None

    # ---------- A. nodes_attention_summary() ----------
    print("== A. nodes_attention_summary() ==")
    if app is not None:
        now = time.time()
        check("staleness constant is 120s",
              app.NODES_ATTENTION_STALE_S == 120)
        check("empty providers -> []",
              app.nodes_attention_summary({}) == [])
        check("non-dict providers -> []",
              app.nodes_attention_summary("junk") == [])
        check("None providers -> [] (real load_providers has no state here)",
              app.nodes_attention_summary(None) == [])

        p = {"srt": prov(now, {"docker": "docker daemon restarted 3 times"},
                         name="srt")}
        h = app.nodes_attention_summary(p)
        check("one fresh degraded node -> 1 entry", len(h) == 1, repr(h))
        check("node name used", h[0]["node"] == "srt", repr(h))
        check("markers listed", h[0]["markers"] == ["docker"], repr(h))
        check("reason carried", "docker daemon restarted 3 times" in
              h[0]["reason"], repr(h))

        # name falls back to provider id
        h = app.nodes_attention_summary(
            {"node-9": prov(now, {"disk": "d"})})
        check("missing name falls back to pid",
              h == [{"node": "node-9", "markers": ["disk"], "reason": "d"}],
              repr(h))

        # staleness: 119s seen -> in, 121s -> out (same rule as the
        # Eventos merge and the /api/providers "online" flag)
        h = app.nodes_attention_summary(
            {"fresh": prov(now - 119, {"a": "x"}),
             "stale": prov(now - 121, {"b": "y"})})
        check("119s-old node included, 121s-old excluded",
              [e["node"] for e in h] == ["fresh"], repr(h))
        h = app.nodes_attention_summary(
            {"never": {"selfwatch": {"degraded": {"a": "x"}}}})
        check("missing last_seen -> stale -> excluded", h == [], repr(h))
        h = app.nodes_attention_summary(
            {"garbage": prov("not-a-time", {"a": "x"})})
        check("garbage last_seen -> excluded", h == [], repr(h))

        # degraded shapes that must not badge
        for label, deg in (("empty dict", {}), ("list", ["docker"]),
                           ("string", "docker"), ("None", None)):
            h = app.nodes_attention_summary(
                {"n": prov(now, deg)})
            check(f"degraded {label} -> no entry", h == [], repr(h))
        h = app.nodes_attention_summary(
            {"n": {"last_seen": now}})
        check("missing selfwatch -> no entry", h == [], repr(h))
        h = app.nodes_attention_summary(
            {"n": {"last_seen": now, "selfwatch": "junk"}})
        check("non-dict selfwatch -> no entry", h == [], repr(h))
        h = app.nodes_attention_summary(
            {"n": prov(now, {"docker": "x"}), "junk": "not-a-dict"})
        check("non-dict provider skipped, good one kept",
              len(h) == 1, repr(h))

        # truncation + empty keys
        h = app.nodes_attention_summary(
            {"n": prov(now, {"": "empty-key", "k" * 50: "v"},
                         name="n" * 60)})
        check("empty marker key skipped, long key truncated",
              h[0]["markers"] == ["k" * 32], repr(h))
        check("node name truncated to 40",
              len(h[0]["node"]) == 40, repr(h))
        h = app.nodes_attention_summary(
            {"n": prov(now, {"docker": "r" * 500})})
        check("reason truncated to 160",
              len(h[0]["reason"]) == 160, repr(h))
        h = app.nodes_attention_summary(
            {"n": prov(now, {"docker": "", "disk": "second"})})
        check("empty first reason falls through to next marker",
              h[0]["reason"] == "second", repr(h))
        h = app.nodes_attention_summary(
            {"n": prov(now, {"docker": 123})})
        check("non-string reason -> empty reason, markers kept",
              h[0] == {"node": "n", "markers": ["docker"], "reason": ""},
              repr(h))

        # several live nodes all surface
        h = app.nodes_attention_summary(
            {"a": prov(now, {"docker": "d1"}, name="srt-a"),
             "b": prov(now, {"mem": "m1"}, name="srt-b")})
        check("two degraded nodes -> two entries", len(h) == 2, repr(h))

        # monkeypatched load_providers feeds the no-arg path
        app.load_providers = lambda: {"m": prov(now, {"docker": "d"},
                                                name="srt-m")}
        try:
            h = app.nodes_attention_summary()
            check("no-arg path uses load_providers()",
                  len(h) == 1 and h[0]["node"] == "srt-m", repr(h))
        finally:
            pass

    # ---------- B. /api/instances contract ----------
    print("== B. /api/instances contract ==")
    try:
        src = open(APP, encoding="utf-8").read()
    except OSError as e:
        bad("read app.py", str(e))
        src = ""
    check("GET /api/instances emits nodes_attention",
          '"nodes_attention": nodes_attention_summary(providers)' in src)
    check("nodes_attention_summary defined",
          re.search(r"def nodes_attention_summary\(providers=None\):",
                    src) is not None)
    check("watchdog_global wiring intact (no clobber)",
          '"watchdog_global": global_watchdog_health()' in src)
    check("staleness rule matches Eventos/providers online (120s)",
          "NODES_ATTENTION_STALE_S = 120" in src)
    check("helper never raises (outer try)",
          "def nodes_attention_summary" in src
          and src.count("except Exception:\n        return []") >= 1)

    # ---------- C. agent cross-check ----------
    print("== C. agent selfwatch_state() cross-check ==")
    try:
        agent = open(AGENT, encoding="utf-8").read()
    except OSError as e:
        bad("read wing/agent.py", str(e))
        agent = ""
    check("agent defines selfwatch_state()",
          "def selfwatch_state(" in agent)
    check("selfwatch_state builds a degraded map from *.degraded files",
          'fn.endswith(".degraded")' in agent
          and 'return {"recoveries": total, "degraded": degraded}' in agent)
    check("agent health endpoint exposes selfwatch_state()",
          'h["selfwatch"] = selfwatch_state()' in agent)
    check("manager heartbeat handler consumes data selfwatch",
          'sw = data.get("selfwatch", {})' in src)

    # ---------- D. dashboard ----------
    print("== D. dashboard badge ==")
    try:
        html = open(HTML, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        html = ""
    check("nodesBadge span lives inside the Nodos button",
          re.search(r'<button class="ghost" id="btnProviders">'
                    r'<i class="ri-server-line"></i>Nodos'
                    r'<span id="nodesBadge" class="nodebadge hidden">'
                    r'</span></button>', html) is not None)
    check("nodebadge CSS rule present",
          ".nodebadge {" in html and "var(--bad)" in html)
    check("refresh() renders nodes_attention",
          "updateNodesBadge(j.nodes_attention)" in html)
    check("badge text uses textContent (no HTML injection)",
          "b.textContent = String(items.length)" in html)
    check("badge block has begin/end markers",
          "// == nodes-attention-badge begin ==" in html
          and "// == nodes-attention-badge end ==" in html)
    m = re.search(
        r"// == nodes-attention-badge begin ==\n(.*?)"
        r"\n// == nodes-attention-badge end ==",
        html, re.S)
    check("badge block extractable", m is not None)
    if m:
        block = m.group(1)
        check("badge block has no emojis", EMOJI_RE.search(block) is None)
        check("badge block has no innerHTML",
              "innerHTML" not in block)
        driver = block + r"""
;(function(){
  var results = [];
  function t(name, cond){ results.push((cond ? 'ok ' : 'FAIL ') + name); }
  // empty list hides the badge
  updateNodesBadge([]);
  t('empty list hides badge', el.classList.hidden === true);
  // null / undefined / non-array hide, never throw
  updateNodesBadge(null);
  t('null hides badge', el.classList.hidden === true);
  updateNodesBadge(undefined);
  t('undefined hides badge', el.classList.hidden === true);
  updateNodesBadge('junk');
  t('non-array hides badge', el.classList.hidden === true);
  // one node shows count 1 + node/marker/reason in tooltip
  updateNodesBadge([{node:'srt', markers:['docker'], reason:'docker daemon restarted 3 times'}]);
  t('one node shows badge', el.classList.hidden === false);
  t('count is 1', el.textContent === '1');
  t('tooltip names the node', el.title.indexOf('srt') !== -1);
  t('tooltip names the marker', el.title.indexOf('docker') !== -1);
  t('tooltip carries the reason', el.title.indexOf('docker daemon restarted 3 times') !== -1);
  // two nodes -> count 2, both in tooltip
  updateNodesBadge([{node:'srt-a', markers:['docker'], reason:'d1'}, {node:'srt-b', markers:['mem'], reason:'m1'}]);
  t('two nodes show count 2', el.textContent === '2');
  t('tooltip carries both nodes', el.title.indexOf('srt-a') !== -1 && el.title.indexOf('srt-b') !== -1);
  // entries without a node are filtered out
  updateNodesBadge([{markers:['docker']}, null, 'junk']);
  t('nodeless entries filtered -> hidden', el.classList.hidden === true);
  // clearing list hides again (auto-clear path)
  updateNodesBadge([{node:'srt', markers:['docker'], reason:'x'}]);
  updateNodesBadge([]);
  t('clearing list hides again', el.classList.hidden === true);
  // missing element never throws
  $ = function(){ return null; };
  try { updateNodesBadge([{node:'srt', markers:['docker'], reason:'x'}]); t('missing element never throws', true); }
  catch(e){ t('missing element never throws', false); }
  console.log(results.join('\n'));
})();
"""
        stub = (
            "var cls = {hidden: true, toggle: function(c, force){ this[c] = !!force; }};\n"
            "var el = {classList: cls, textContent: '', title: ''};\n"
            "function $(id){ return id === 'nodesBadge' ? el : null; }\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as f:
            f.write(stub + driver)
            path = f.name
        try:
            p = subprocess.run(["node", path], capture_output=True,
                               text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            bad("node driver run", str(e))
            p = None
        finally:
            os.unlink(path)
        if p is not None:
            check("node driver exited 0", p.returncode == 0,
                  p.stderr[:200])
            for line in (p.stdout or "").splitlines():
                if line.startswith("ok "):
                    ok("node: " + line[3:])
                elif line.startswith("FAIL "):
                    bad("node: " + line[5:])

    print(f"PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
