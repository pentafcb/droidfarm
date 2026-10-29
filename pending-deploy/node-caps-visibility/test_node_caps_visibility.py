#!/usr/bin/env python3
"""node-caps-visibility: wing agent 1.0.27 host caps on the heartbeat ->
manager sanitizes + stores -> /api/providers emits -> Nodes modal renders.

Hermetic: no network, no docker, no systemd. Imports the real wing/agent.py
and the real manager/app.py helpers (source contracts via inspect).
"""
import importlib.util
import json
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PASS, FAIL = 0, 0


def ok(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name)


def load_agent():
    path = os.path.join(REPO, "wing", "agent.py")
    spec = importlib.util.spec_from_file_location("wing_agent_1027", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def agent_src():
    with open(os.path.join(REPO, "wing", "agent.py")) as f:
        return f.read()


def mgr_src():
    with open(os.path.join(REPO, "manager", "app.py")) as f:
        return f.read()


def html_src():
    with open(os.path.join(REPO, "manager", "static", "index.html")) as f:
        return f.read()


A = load_agent()

# ---- A: VERSION exact bump + manager gate compat ----
ok("A1 VERSION is 1.0.27+ (1.0.28 adds proxy-aware rate-limit keys)",
   A.VERSION in ("1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"))
ok("A2 parse_agent_version ordering: 1.0.27 > 1.0.26",
   A.parse_agent_version("1.0.27") > A.parse_agent_version("1.0.26"))
_m = re.search(r'_version_at_least\(ver,\s*"([0-9.]+)"\)', mgr_src())
_gate = _m.group(1) if _m else None
def _vt(v):
    return tuple(int(x) for x in str(v).split("."))
ok("A3 manager gate accepts the current agent version (gate=%s)" % _gate,
   _gate is not None and _vt(A.VERSION) >= _vt(_gate))

# ---- B: sw_host_caps units ----
ok("B1 windows -> all-None",
   (lambda m: (setattr(m, "IS_WINDOWS", True), m.sw_host_caps())[1])(A)
   == {"loop_devices": None, "image_present": None})
A.IS_WINDOWS = False
orig_loop, orig_img = A.sw_count_loop_devices, A.sw_image_present
A.sw_count_loop_devices = lambda loop_dir="/dev": 64
A.sw_image_present = lambda: True
caps = A.sw_host_caps()
ok("B2 linux healthy shape", caps == {"loop_devices": 64, "image_present": True})
A.sw_image_present = lambda: False
ok("B3 image missing tri-state", A.sw_host_caps()["image_present"] is False)
A.sw_image_present = lambda: None
ok("B4 image unknown tri-state", A.sw_host_caps()["image_present"] is None)


def boom(*a, **k):
    raise RuntimeError("x")


A.sw_count_loop_devices = boom
ok("B5 never-raises on helper failure",
   A.sw_host_caps() == {"loop_devices": None, "image_present": None})
A.sw_count_loop_devices, A.sw_image_present = orig_loop, orig_img
A.IS_WINDOWS = False

# ---- C: heartbeat payload carries host_caps ----
src = agent_src()
m = re.search(r"def build_heartbeat_payload\(\):(.*?)(?=\ndef |\nclass )",
              src, re.S)
body = m.group(1)
ok("C1 payload sets host_caps from sw_host_caps()",
   'payload["host_caps"] = sw_host_caps()' in body)
ok("C2 payload write is exception-guarded",
   "try:" in body and "except Exception:" in body)
ok("C3 sw_host_caps defined with the documented contract",
   re.search(r"def sw_host_caps\(\):.*?loop_devices.*?image_present",
             src, re.S) is not None)

# ---- D: manager clean_agent_host_caps ----
sys.path.insert(0, os.path.join(REPO, "manager"))
import app as M  # noqa: E402  (real manager module; heavy but import-safe)

ok("D1 valid dict passes through",
   M.clean_agent_host_caps({"loop_devices": 64, "image_present": True})
   == {"loop_devices": 64, "image_present": True})
ok("D2 image tri-state preserved",
   M.clean_agent_host_caps({"loop_devices": 8, "image_present": False})["image_present"] is False
   and M.clean_agent_host_caps({"loop_devices": 8, "image_present": None})["image_present"] is None)
ok("D3 non-dict -> all None",
   M.clean_agent_host_caps("x") == {"loop_devices": None, "image_present": None}
   and M.clean_agent_host_caps(None) == {"loop_devices": None, "image_present": None})
ok("D4 garbage loop -> None (float str, negative, huge)",
   M.clean_agent_host_caps({"loop_devices": "abc", "image_present": True})["loop_devices"] is None
   and M.clean_agent_host_caps({"loop_devices": -1, "image_present": True})["loop_devices"] is None
   and M.clean_agent_host_caps({"loop_devices": 10 ** 9, "image_present": True})["loop_devices"] is None)
ok("D5 non-bool image -> None", M.clean_agent_host_caps(
    {"loop_devices": 64, "image_present": "yes"})["image_present"] is None)
ok("D6 string-int loop coerced",
   M.clean_agent_host_caps({"loop_devices": "64", "image_present": True})["loop_devices"] == 64)
ok("D7 never-raises", M.clean_agent_host_caps(object()) ==
   {"loop_devices": None, "image_present": None})

# ---- E: heartbeat handler wiring (source contract) ----
msrc = mgr_src()
ok("E1 heartbeat stores sanitized host_caps with updated ts",
   'p["host_caps"] = caps' in msrc
   and 'caps = clean_agent_host_caps(data.get("host_caps"))' in msrc
   and 'caps["updated"] = time.time()' in msrc)
ok("E2 host_caps stored only inside the heartbeat route",
   msrc.count('p["host_caps"]') == 1)

# ---- F: /api/providers emission (source contract) ----
ok("F1 remote branch emits agent_loop_devices + agent_image_present",
   'caps = p.get("host_caps")' in msrc
   and '"agent_loop_devices": (caps if isinstance(caps, dict) else {}).get("loop_devices")' in msrc
   and '"agent_image_present": (caps if isinstance(caps, dict) else {}).get("image_present")' in msrc)
ok("F2 local pseudo-provider reports None/None (no data, not zero)",
   '"agent_loop_devices": None, "agent_image_present": None' in msrc)

# ---- G: dashboard contract ----
h = html_src()
ok("G1 modal: image-ready chip (dim)", "imagen lista" in h)
ok("G2 modal: image-missing chip (warn)", "imagen no construida" in h)
ok("G3 modal: loop count chip", ">loop: ' + esc(String(p.agent_loop_devices))" in h)
ok("G4 modal: loop warns below 60", "p.agent_loop_devices < 60" in h)
ok("G5 modal: caps skipped for the local pseudo-provider",
   "p.id !== 'local' && p.agent_image_present" in h
   and "p.id !== 'local' && p.agent_loop_devices" in h)
ok("G6 modal: unknown caps render nothing (strict ===/!= null checks)",
   "p.agent_image_present === true" in h
   and "p.agent_image_present === false" in h)
ok("G7 info modal: Imagen Waydroid lines", "Imagen Waydroid:</b>lista" in h
   and "Imagen Waydroid:</b><span" in h)
ok("G8 info modal: loop line with low-pool warning", "Dispositivos loop:</b>" in h
   and "pocos — el arranque puede fallar" in h)
ok("G9 values are esc()-escaped", h.count("esc(String(p.agent_loop_devices))") >= 2)
ok("G10 no emojis in the new lines",
   not re.search(r"imagen (lista|no construida)[^\s<]*[\U0001F300-\U0001FAFF]", h))

# ---- H: node-driven render of the caps chips ----
node_js = r"""
const esc = s => String(s||'').replace(/[&<>"]/g, c =>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function chips(p){
  const hci = (p.id !== 'local' && p.agent_image_present === true) ? '<b>IMG_OK</b>' : '';
  const hcm = (p.id !== 'local' && p.agent_image_present === false) ? '<b>IMG_MISS</b>' : '';
  const hcl = (p.id !== 'local' && p.agent_loop_devices != null) ? '<b>LOOP' + (p.agent_loop_devices < 60 ? '_LOW' : '') + ':' + esc(String(p.agent_loop_devices)) + '</b>' : '';
  return hci + hcm + hcl;
}
const cases = [
  [{id:'n1', agent_image_present:true, agent_loop_devices:64}, '<b>IMG_OK</b><b>LOOP:64</b>'],
  [{id:'n1', agent_image_present:false, agent_loop_devices:8}, '<b>IMG_MISS</b><b>LOOP_LOW:8</b>'],
  [{id:'n1', agent_image_present:null, agent_loop_devices:null}, ''],
  [{id:'local', agent_image_present:false, agent_loop_devices:8}, ''],
  [{id:'n1', agent_image_present:true, agent_loop_devices:60}, '<b>IMG_OK</b><b>LOOP:60</b>'],
];
let bad = 0;
for (const [p, want] of cases) { if (chips(p) !== want) { bad++; console.log('MISMATCH', JSON.stringify(p), chips(p)); } }
console.log(bad === 0 ? 'NODE_OK' : 'NODE_BAD');
"""
import subprocess
r = subprocess.run(["node", "-e", node_js], capture_output=True, text=True,
                   timeout=30)
ok("H1 caps chip state machine (built/missing/low/unknown/local)",
   "NODE_OK" in r.stdout)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
