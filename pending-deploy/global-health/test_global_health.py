#!/usr/bin/env python3
"""Tests for global watchdog health surfacing (global DEGRADED -> dashboard banner).

GAP: per-instance watchdog DEGRADED markers already surface as dashboard
card badges (instance-health), but the watchdog's GLOBAL markers — manager,
docker, disk, mem budgets tripped, farm-wide auto-recovery paused — were
only transient Eventos log lines. Once the log scrolled past, the dashboard
kept showing green cards while the farm was paused. manager/app.py now
reads the four global markers into GET /api/instances as watchdog_global,
and the dashboard renders one red banner above the grid.

Covers (hermetic):
  A. manager.global_watchdog_health() — marker parsing (fake DF_ST dir)
  B. /api/instances contract — watchdog_global wired into the response
  C. watchdog cross-check — df-watchdog.sh really writes all four markers
  D. dashboard contract + behavioral node test of the extracted banner block
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
HTML = os.path.join(REPO, "manager", "static", "index.html")
WD = os.path.join(REPO, "waydroid-image", "df-watchdog.sh")

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
    print("global-health tests")
    try:
        app = load_app()
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        app = None

    # ---------- A. global_watchdog_health() ----------
    print("== A. global_watchdog_health() ==")
    if app is not None:
        with tempfile.TemporaryDirectory() as st:
            os.environ["DF_ST"] = st
            # Fixture models a FIRING watchdog: a fresh heartbeat, so the
            # heartbeat-banner entry never leaks into marker assertions.
            with open(os.path.join(st, "heartbeat"), "w") as f:
                f.write("")
            h = app.global_watchdog_health()
            check("no markers -> []", h == [], repr(h))

            with open(os.path.join(st, "manager.degraded"), "w") as f:
                f.write("manager service restarted 3 times in last 3600s - auto-recovery paused\n")
            with open(os.path.join(st, "docker.degraded"), "w") as f:
                f.write("docker daemon restarted 3 times in last 3600s - auto-recovery paused\n")
            with open(os.path.join(st, "disk.degraded"), "w") as f:
                f.write("disk auto-prune ran 3 times in last 3600s without relieving pressure - auto-prune paused\n")
            with open(os.path.join(st, "mem.degraded"), "w") as f:
                f.write("host MemAvailable 512MB - instance limits overcommit RAM (needs human)\n")
            # per-instance markers + unrelated files must NOT leak in
            with open(os.path.join(st, "rbx1.degraded"), "w") as f:
                f.write("container restarted 7 times\n")
            with open(os.path.join(st, "notes.txt"), "w") as f:
                f.write("manager.degraded\n")
            h = app.global_watchdog_health()
            check("all four markers -> 4 entries",
                  len(h) == 4, repr(h))
            check("entries in marker order",
                  [e["key"] for e in h] == ["manager", "docker", "disk", "memory"],
                  repr(h))
            check("manager reason exact",
                  h[0]["reason"] == "manager service restarted 3 times in last 3600s - auto-recovery paused",
                  repr(h))
            check("memory key maps mem.degraded file",
                  h[3]["key"] == "memory" and "MemAvailable" in h[3]["reason"],
                  repr(h))

            # empty marker -> generic reason
            with open(os.path.join(st, "disk.degraded"), "w") as f:
                pass
            h = app.global_watchdog_health()
            disk = [e for e in h if e["key"] == "disk"][0]
            check("empty marker -> generic reason",
                  disk["reason"] == "disk auto-prune paused", repr(disk))

            # multiline -> first line only; long line -> 200 chars
            with open(os.path.join(st, "mem.degraded"), "w") as f:
                f.write("first line\nsecond line\n")
            h = app.global_watchdog_health()
            mem = [e for e in h if e["key"] == "memory"][0]
            check("multiline -> first line only",
                  mem["reason"] == "first line", repr(mem))
            with open(os.path.join(st, "mem.degraded"), "w") as f:
                f.write("x" * 500 + "\n")
            h = app.global_watchdog_health()
            mem = [e for e in h if e["key"] == "memory"][0]
            check("long reason truncated to 200",
                  len(mem["reason"]) == 200, repr(len(mem["reason"])))

            # a directory named like a marker is not a marker
            os.remove(os.path.join(st, "docker.degraded"))
            os.mkdir(os.path.join(st, "docker.degraded"))
            h = app.global_watchdog_health()
            check("directory marker skipped",
                  all(e["key"] != "docker" for e in h), repr(h))

            # explicit st_dir param overrides DF_ST
            with tempfile.TemporaryDirectory() as st2:
                with open(os.path.join(st2, "heartbeat"), "w") as f:
                    f.write("")
                with open(os.path.join(st2, "manager.degraded"), "w") as f:
                    f.write("from st2\n")
                h = app.global_watchdog_health(st_dir=st2)
                check("st_dir param honored",
                      h == [{"key": "manager", "reason": "from st2"}], repr(h))

            # missing dir -> [] (never raises)
            h = app.global_watchdog_health(
                st_dir=os.path.join(st, "does-not-exist"))
            check("missing dir -> []", h == [], repr(h))
        # never raises even with DF_ST unset to garbage
        os.environ.pop("DF_ST", None)
        try:
            h = app.global_watchdog_health(st_dir="/nonexistent-df-st-xyz")
            check("bad dir never raises", h == [], repr(h))
        except Exception as e:  # noqa: BLE001
            bad("bad dir never raises", str(e))

    # ---------- B. /api/instances contract ----------
    print("== B. /api/instances contract ==")
    try:
        src = open(APP, encoding="utf-8").read()
    except OSError as e:
        bad("read app.py", str(e))
        src = ""
    check("GLOBAL_WATCHDOG_MARKERS has all four files",
          all(f in src for f in ("manager.degraded", "docker.degraded",
                                 "disk.degraded", "mem.degraded")))
    check("GET /api/instances emits watchdog_global",
          '"watchdog_global": global_watchdog_health()' in src)
    check("global_watchdog_health defined",
          re.search(r"def global_watchdog_health\(st_dir=None\):", src) is not None)

    # ---------- C. watchdog cross-check ----------
    print("== C. watchdog writes all four markers ==")
    try:
        wd = open(WD, encoding="utf-8").read()
    except OSError as e:
        bad("read df-watchdog.sh", str(e))
        wd = ""
    check("watchdog references manager.degraded",
          "manager.degraded" in wd)
    check("watchdog references docker.degraded",
          "docker.degraded" in wd)
    check("watchdog references disk.degraded",
          "disk.degraded" in wd)
    check("watchdog references mem.degraded",
          "mem.degraded" in wd)
    check("watchdog writes manager marker via cb_guard",
          'cb_guard "manager"' in wd)
    check("watchdog writes docker marker via cb_guard",
          'cb_guard "docker"' in wd)

    # ---------- D. dashboard ----------
    print("== D. dashboard banner ==")
    try:
        html = open(HTML, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        html = ""
    check("globalBanner element exists",
          'id="globalBanner"' in html)
    check("globalBanner reuses connbanner style",
          'id="globalBanner" class="connbanner' in html)
    check("globalBanner sits before connBanner (connBanner still "
          "immediately precedes the grid — conn-banner suite contract)",
          re.search(r'<div id="globalBanner" class="connbanner hidden">'
                    r'<i class="ri-alert-line"></i><span></span></div>'
                    r'<div id="connBanner" class="connbanner hidden">',
                    html) is not None)
    check("refresh() renders watchdog_global",
          "updateGlobalBanner(j.watchdog_global)" in html)
    check("all four Spanish labels present",
          all(s in html for s in ("Servicio del panel", "Docker", "Disco",
                                  "Memoria")))
    check("banner text uses textContent (no HTML injection)",
          "b.querySelector('span').textContent" in html)
    check("banner block has begin/end markers",
          "// == global-health-banner begin ==" in html
          and "// == global-health-banner end ==" in html)
    m = re.search(
        r"// == global-health-banner begin ==\n(.*?)"
        r"\n// == global-health-banner end ==",
        html, re.S)
    check("banner block extractable", m is not None)
    if m:
        block = m.group(1)
        check("banner block has no emojis", EMOJI_RE.search(block) is None)
        driver = block + r"""
;(function(){
  var results = [];
  function t(name, cond){ results.push((cond ? 'ok ' : 'FAIL ') + name); }
  // empty list hides the banner
  updateGlobalBanner([]);
  t('empty list hides banner', el.classList.hidden === true);
  // null / non-array input hides, never throws
  updateGlobalBanner(null);
  t('null hides banner', el.classList.hidden === true);
  updateGlobalBanner(undefined);
  t('undefined hides banner', el.classList.hidden === true);
  // one marker shows with its Spanish label + paused wording
  updateGlobalBanner([{key:'manager', reason:'manager service restarted 3 times'}]);
  t('one marker shows banner', el.classList.hidden === false);
  t('banner names the subsystem in Spanish', span.textContent.indexOf('Servicio del panel') !== -1);
  t('banner says auto-recovery paused', span.textContent.indexOf('recuperación automática pausada') !== -1);
  t('reason lands in the tooltip', el.title.indexOf('manager service restarted 3 times') !== -1);
  // several markers join labels; unknown keys fall back to the raw key
  updateGlobalBanner([{key:'disk', reason:'d1'}, {key:'memory', reason:'m1'}, {key:'weird', reason:'w1'}]);
  t('several markers stay shown', el.classList.hidden === false);
  t('labels joined', span.textContent.indexOf('Disco, Memoria, weird') !== -1);
  t('tooltip carries all reasons', el.title.indexOf('d1') !== -1 && el.title.indexOf('m1') !== -1 && el.title.indexOf('w1') !== -1);
  // entries without a key are filtered out
  updateGlobalBanner([{reason:'no key'}, null, 'junk']);
  t('keyless entries filtered -> hidden', el.classList.hidden === true);
  // empty list again hides (auto-clear path)
  updateGlobalBanner([{key:'docker', reason:'x'}]);
  updateGlobalBanner([]);
  t('clearing list hides again', el.classList.hidden === true);
  // missing element never throws
  $ = function(){ return null; };
  try { updateGlobalBanner([{key:'disk', reason:'x'}]); t('missing element never throws', true); }
  catch(e){ t('missing element never throws', false); }
  console.log(results.join('\n'));
})();
"""
        stub = (
            "var cls = {hidden: true, toggle: function(c, force){ this[c] = !!force; }};\n"
            "var span = {textContent: ''};\n"
            "var el = {classList: cls, querySelector: function(){ return span; }, title: ''};\n"
            "function $(id){ return id === 'globalBanner' ? el : null; }\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(stub + driver)
            path = f.name
        try:
            p = subprocess.run(["node", path], capture_output=True, text=True,
                               timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            bad("node driver run", str(e))
            p = None
        finally:
            os.unlink(path)
        if p is not None:
            check("node driver exited 0", p.returncode == 0, p.stderr[:200])
            for line in (p.stdout or "").splitlines():
                if line.startswith("ok "):
                    ok("node: " + line[3:])
                elif line.startswith("FAIL "):
                    bad("node: " + line[5:])

    print(f"PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
