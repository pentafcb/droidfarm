#!/usr/bin/env python3
"""Tests for the Nodes modal connectivity error line (nodes-conn-err).

GAP: refreshProviders() (the Nodes modal list) swallowed every failure with
a bare `catch(e){}` — a dropped tunnel or wedged manager left an empty or
stale node list with zero indication. The grid got its own conn-banner; the
Nodes modal had no equivalent.
manager/static/index.html now shows an honest red error line
("Sin respuesta del servidor — no se pudieron cargar los nodos.") when the
/api/providers fetch fails, and hides it on the next successful load.

Covers (hermetic):
  A. Behavioral: extract the marked nodes-conn-err block from index.html,
     run it in node with a stubbed $/elements, drive provConnErrShow /
     provConnErrHide and assert the hidden -> shown -> hidden transitions
     + exact text + missing-element tolerance.
  B. Dashboard contract: provConnErr element in the provModal markup
     (hidden by default, --bad palette), wiring inside refreshProviders()
     (hide on success, show in the catch — no longer a bare catch),
     Spanish label, no emojis, textContent-only for the message.
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
INDEX = os.path.join(REPO, "manager", "static", "index.html")

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
    "[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\u2B00-\u2BFF\uFE0F]")

MSG = "Sin respuesta del servidor — no se pudieron cargar los nodos."


def main():
    print("nodes-conn-err tests")
    try:
        src = open(INDEX, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        return 1

    # ---------- A. behavioral test of the extracted block in node ----------
    m = re.search(
        r"// == nodes-conn-err begin ==\n(.*?)\n// == nodes-conn-err end ==",
        src, re.S)
    check("nodes-conn-err block has begin/end markers", m is not None)
    if m:
        driver = m.group(1) + r"""
;(function(){
  var results = [];
  function t(name, cond){ results.push((cond ? 'ok ' : 'FAIL ') + name); }
  // initial: error line hidden, list untouched
  t('initial err hidden', err.style.display === 'none');
  t('initial list intact', list.innerHTML === '<div>stale</div>');
  // show: list cleared, message set, line visible
  provConnErrShow();
  t('show sets exact Spanish text', err.textContent === MSG);
  t('show makes the line visible', err.style.display === '');
  t('show clears the node list', list.innerHTML === '');
  // show is idempotent
  provConnErrShow();
  t('show is idempotent', err.textContent === MSG && err.style.display === '');
  // hide: line hidden again, list not touched
  list.innerHTML = '<div>fresh</div>';
  provConnErrHide();
  t('hide hides the line', err.style.display === 'none');
  t('hide does not clear the list', list.innerHTML === '<div>fresh</div>');
  // full cycle
  provConnErrShow(); provConnErrHide();
  t('show->hide cycle ends hidden', err.style.display === 'none');
  // missing elements never throw (page markup always has them; guards only)
  $ = function(){ return null; };
  try { provConnErrShow(); provConnErrHide(); t('missing elements never throw', true); }
  catch(e){ t('missing elements never throw', false); }
  console.log(results.join('\n'));
})();
""".replace("MSG", '"' + MSG + '"')
        stub = (
            "var err = {style: {display: 'none'}, textContent: ''};\n"
            "var list = {innerHTML: '<div>stale</div>'};\n"
            "function $(id){ return id === 'provConnErr' ? err : (id === 'provList' ? list : null); }\n"
            "var MSG = " + '"' + MSG + '";\n'
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

    # ---------- B. dashboard contract ----------
    body = src

    # element: in the provModal, right after the provList div, hidden, --bad
    fm = re.search(
        r'<div id="provList"></div><p class="hint" id="provConnErr" '
        r'style="display:none;color:var\(--bad\)"></p>', body)
    check("provConnErr element sits right after provList in the modal",
          fm is not None)

    # wiring inside refreshProviders(): hide on success, show in the catch
    rm = re.search(r"async function refreshProviders\(\)\{(.*?)\nfunction esc\(",
                   body, re.S)
    check("refreshProviders() body extractable", rm is not None)
    if rm:
        rbody = rm.group(1)
        check("success path hides the error line",
              "provConnErrHide();" in rbody)
        check("catch shows the error line",
              "} catch(e){ provConnErrShow(); }" in rbody)
        check("the old bare OUTER catch is gone",
              "} catch(e){}\n}" not in rbody)

    # block-level contracts
    if m:
        block = m.group(1)
        check("block uses textContent for the message",
              "textContent" in block)
        check("block clears only the static list markup",
              block.count("innerHTML") == 1)
        check("block label is Spanish", "Sin respuesta del servidor" in block)
        check("block has no emojis", EMOJI_RE.search(block) is None)
        check("both helpers guard missing elements",
              block.count("if (el)") == 1 and
              block.count("if (e)") == 1 and
              block.count("if (!e) return;") == 1)

    # the message line in the page has no emojis
    msg_lines = [l for l in body.splitlines()
                 if "no se pudieron cargar los nodos" in l]
    check("error message present in page", len(msg_lines) >= 1)
    check("error message has no emojis",
          msg_lines and all(EMOJI_RE.search(l) is None for l in msg_lines))

    # create/fleet-create dropdown catches intentionally left as graceful
    # degradation (they still offer auto/local) — only the observability
    # surface (the modal) needed the honest error
    check("no stray provConnErr refs outside the modal + wiring",
          body.count("provConnErr") == 7)

    print(f"{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
