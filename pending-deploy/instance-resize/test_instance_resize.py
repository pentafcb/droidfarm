#!/usr/bin/env python3
"""Unit tests for instance-resize: live per-instance RAM/CPU resize.

Per-instance resource limits were create-time only (fleet-create plan /
_create_instance). There was no way to change an instance's RAM/CPUs after
creation without raw `docker update` on the VPS. Now: POST
/api/instances/<name>/resize (manager, auth-gated, route-name-gated) ->
docker update live (no restart) on the main node, or proxied to the wing
agent (1.0.31) for remote instances. A shrink below live usage is refused
(it would OOM-kill the running session); unknown usage fails open.

Hermetic: real manager/app.py + real wing/agent.py imports, faked docker /
instances / wing_request / usage caches; dashboard block driven in node.
"""
import importlib.util
import json
import copy
import os
import re
import subprocess
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "manager"))
import app

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


def _load_agent():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "wing", "agent.py")
    spec = importlib.util.spec_from_file_location("wing_agent_resize", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agent = _load_agent()

# ---------- A. version ------------------------------------------------------
check("A1 agent VERSION is 1.0.31+ (1.0.32 adds boot recovery)",
      agent.VERSION in ("1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), repr(agent.VERSION))
check("A2 parse_agent_version ordering: 1.0.31 > 1.0.30",
      agent.parse_agent_version("1.0.31") > agent.parse_agent_version("1.0.30"))
check("A3 manager >=1.0.2 gate accepts 1.0.31",
      app._version_at_least("1.0.31", "1.0.2")
      and not app._version_at_least("1.0.1", "1.0.2"))

# ---------- B. validate_resize_params ---------------------------------------
clean, err = app.validate_resize_params({})
check("B1 empty body -> required error",
      clean is None and err == "memory_mb or cpus required", repr(err))
clean, err = app.validate_resize_params({"memory_mb": 1024, "cpus": 1.5})
check("B2 both fields valid", err is None and clean == {"memory_mb": 1024, "cpus": 1.5},
      repr((clean, err)))
clean, err = app.validate_resize_params({"memory_mb": "2048"})
check("B3 string memory coerced", err is None and clean["memory_mb"] == 2048,
      repr((clean, err)))
clean, err = app.validate_resize_params({"cpus": "2.5"})
check("B4 string cpus coerced", err is None and clean["cpus"] == 2.5,
      repr((clean, err)))
clean, err = app.validate_resize_params({"memory_mb": 512})
check("B5 memory below floor rejected", clean is None and "1024" in (err or ""),
      repr(err))
clean, err = app.validate_resize_params({"memory_mb": 65536})
check("B6 memory above ceiling rejected", clean is None and "32768" in (err or ""),
      repr(err))
clean, err = app.validate_resize_params({"cpus": 0.1})
check("B7 cpus below floor rejected", clean is None and "0.5" in (err or ""),
      repr(err))
clean, err = app.validate_resize_params({"cpus": 64})
check("B8 cpus above ceiling rejected", clean is None and "32" in (err or ""),
      repr(err))
clean, err = app.validate_resize_params({"memory_mb": "abc"})
check("B9 garbage memory -> invalid", clean is None and err == "invalid cpus/memory",
      repr(err))
clean, err = app.validate_resize_params(None)
check("B10 non-dict body -> required error",
      clean is None and err == "memory_mb or cpus required", repr(err))
clean, err = app.validate_resize_params({"memory_mb": True})
check("B11 bool memory rejected by bounds", clean is None, repr((clean, err)))

# ---------- C. resize_local_instance ----------------------------------------
FAKE_INST = {"rbx1": {"name": "rbx1", "provider": "local",
                      "memory_mb": 2048, "cpus": 2, "ip": "10.20.0.11"}}


def drive_local(name, memory_mb, cpus, instances=None, rc=0, usage=None):
    """Run the real resize_local_instance with fakes. Returns (code, resp, seen)."""
    seen = {"cmds": [], "saved": None, "invalidated": []}
    inst = copy.deepcopy(instances if instances is not None else FAKE_INST)

    def fake_run(cmd, timeout=30):
        seen["cmds"].append(cmd)
        return rc, "", "" if rc == 0 else "Error: No such container"

    with mock.patch.object(app, "load_instances", lambda: dict(inst)), \
         mock.patch.object(app, "save_instances",
                           lambda d: seen.update(saved=dict(d))), \
         mock.patch.object(app, "run", fake_run), \
         mock.patch.object(app, "instance_resources", lambda n: usage), \
         mock.patch.object(app, "invalidate_instance_snapshot",
                           lambda n: seen["invalidated"].append(n)):
        code, resp = app.resize_local_instance(name, memory_mb, cpus)
    return code, resp, seen


code, resp, seen = drive_local("rbx1", 1024, 1)
check("C1 happy path 200", code == 200 and resp.get("ok") is True, repr((code, resp)))
check("C2 docker update argv exact",
      # int cpus 1 is coerced to float 1.0 by the resize sink gate (t163);
      # the live route already coerced to float, so this is live-identical.
      seen["cmds"] == ["docker update --memory 1024m --cpus 1.0 wd-rbx1"],
      repr(seen["cmds"]))
check("C3 instances.json updated",
      seen["saved"] and seen["saved"]["rbx1"]["memory_mb"] == 1024
      and seen["saved"]["rbx1"]["cpus"] == 1, repr(seen["saved"]))
check("C4 snapshot invalidated", seen["invalidated"] == ["rbx1"],
      repr(seen["invalidated"]))
check("C5 response carries new limits",
      resp.get("memory_mb") == 1024 and resp.get("cpus") == 1, repr(resp))

code, resp, seen = drive_local("rbx1", 4096, None)
check("C6 memory-only: no --cpus flag",
      seen["cmds"] == ["docker update --memory 4096m wd-rbx1"], repr(seen["cmds"]))
check("C7 memory-only keeps old cpus in json",
      seen["saved"]["rbx1"]["cpus"] == 2, repr(seen["saved"]))

code, resp, seen = drive_local("rbx1", None, 4)
check("C8 cpus-only: no --memory flag",
      # int cpus 4 coerced to 4.0 by the sink gate (t163), as in C2.
      seen["cmds"] == ["docker update --cpus 4.0 wd-rbx1"], repr(seen["cmds"]))

code, resp, seen = drive_local("rbx1", 1024, 1, rc=1)
check("C9 docker failure -> 500", code == 500 and "docker update failed" in resp.get("error", ""),
      repr((code, resp)))
check("C10 docker failure does NOT write instances.json", seen["saved"] is None,
      repr(seen["saved"]))
check("C11 docker failure does NOT invalidate snapshot", seen["invalidated"] == [],
      repr(seen["invalidated"]))

code, resp, seen = drive_local("nope", 1024, 1)
check("C12 unknown instance -> 404", code == 404, repr((code, resp)))
check("C13 unknown instance: docker never called", seen["cmds"] == [],
      repr(seen["cmds"]))

code, resp, seen = drive_local("x;touch /tmp/pwned", 1024, 1)
check("C14 hostile name -> 404, no shell", code == 404 and seen["cmds"] == [],
      repr((code, resp, seen["cmds"])))

code, resp, seen = drive_local("rbx1", 1024, 1,
                               usage={"mem_used_mb": 1500.0, "cpu_pct": 10.0})
check("C15 shrink below live usage refused",
      code == 400 and "below current usage" in resp.get("error", ""),
      repr((code, resp)))
check("C16 usage-guard refusal: docker never called", seen["cmds"] == [],
      repr(seen["cmds"]))

code, resp, seen = drive_local("rbx1", 2048, 1,
                               usage={"mem_used_mb": 1500.0, "cpu_pct": 10.0})
check("C17 shrink above usage proceeds", code == 200, repr((code, resp)))

code, resp, seen = drive_local("rbx1", 2048, 1, usage=None)
check("C18 unknown usage fails open", code == 200, repr((code, resp)))
# NOTE (t163): this case used to pass memory 512; the sink gate now
# correctly rejects 512 as below the 1024 floor, so the fails-open
# probe uses an in-bounds value — the usage guard, not the bounds
# gate, is what is under test here.

remote_inst = {"srt1": {"name": "srt1", "provider": "p_x", "memory_mb": 2048,
                        "cpus": 2, "ip": "10.20.0.11"}}
code, resp, seen = drive_local("srt1", 1024, 1, instances=remote_inst)
check("C19 remote-provider record -> 409", code == 409, repr((code, resp)))

# ---------- D. resize_remote_instance ---------------------------------------
def drive_remote(name, memory_mb, cpus, instances=None, providers=None,
                 wing=(200, {"ok": True})):
    seen = {"saved": None, "invalidated": [], "wing": []}
    inst = copy.deepcopy(instances if instances is not None else remote_inst)
    provs = providers if providers is not None else {
        "p_x": {"id": "p_x", "last_seen": 9999999999}}

    def fake_wing(p, method, path, data=None, timeout=30, attempts=3):
        seen["wing"].append((method, path, data))
        return wing

    with mock.patch.object(app, "load_instances", lambda: dict(inst)), \
         mock.patch.object(app, "load_providers", lambda: dict(provs)), \
         mock.patch.object(app, "save_instances",
                           lambda d: seen.update(saved=dict(d))), \
         mock.patch.object(app, "wing_request", fake_wing), \
         mock.patch.object(app, "invalidate_instance_snapshot",
                           lambda n: seen["invalidated"].append(n)):
        code, resp = app.resize_remote_instance(name, memory_mb, cpus, "p_x")
    return code, resp, seen


code, resp, seen = drive_remote("srt1", 1024, 1)
check("D1 remote happy path 200", code == 200 and resp.get("ok") is True,
      repr((code, resp)))
check("D2 agent path + payload exact",
      seen["wing"] == [("POST", "/api/agent/instances/srt1/resize",
                        {"memory_mb": 1024, "cpus": 1})], repr(seen["wing"]))
check("D3 remote json updated",
      seen["saved"] and seen["saved"]["srt1"]["memory_mb"] == 1024
      and seen["saved"]["srt1"]["cpus"] == 1, repr(seen["saved"]))
check("D4 remote response carries provider",
      resp.get("provider") == "p_x", repr(resp))

code, resp, seen = drive_remote("srt1", 1024, 1, providers={})
check("D5 unknown node -> 404", code == 404, repr((code, resp)))

code, resp, seen = drive_remote("srt1", 1024, 1,
                                providers={"p_x": {"id": "p_x", "last_seen": 1}})
check("D6 offline node -> 409, no wing call", code == 409 and seen["wing"] == [],
      repr((code, resp)))

code, resp, seen = drive_remote("srt1", 1024, 1, wing=(500, {"error": "boom"}))
check("D7 agent 500 passes through", code == 500, repr((code, resp)))
check("D8 agent failure: json untouched", seen["saved"] is None, repr(seen["saved"]))

code, resp, seen = drive_remote("nope", 1024, 1)
check("D9 unknown instance -> 404", code == 404 and seen["wing"] == [],
      repr((code, resp)))

# ---------- E. manager route contract ---------------------------------------
SRC = open(os.path.join(os.path.dirname(__file__), "..", "..", "manager",
                        "app.py")).read()
check("E1 resize route regex in do_POST",
      're.fullmatch(r"/api/instances/([^/]+)/resize", path)' in SRC)
check("E2 route gates name via valid_instance_name",
      SRC.index('"/api/instances/([^/]+)/resize"') <
      SRC.index("if not valid_instance_name(name):\n"
                '                return self._json(404, {"error": "instance not found"})\n'
                "            try:\n"
                '                body = json.loads(self._read_body() or "{}")'))
# global auth gate precedes the instance routes
_auth_pos = SRC.index("if not self._authed():\n"
                      '            return self._json(401, {"error": "unauthorized"})\n\n'
                      "        # ---- provider management")
_resize_pos = SRC.index('"/api/instances/([^/]+)/resize"')
check("E3 resize route sits behind the global POST auth gate", _auth_pos < _resize_pos)
check("E4 both cores wired to the route",
      "resize_local_instance(name, clean[" in SRC
      and "resize_remote_instance(name, clean[" in SRC)

# ---------- F. agent resize_instance_linux ----------------------------------
def drive_agent_resize(name, memory_mb, cpus, instances=None, rc=0,
                       usage=None):
    seen = {"argv": [], "saved": None, "invalidated": []}
    inst = copy.deepcopy(instances if instances is not None else
                         {"rbx1": {"memory_mb": 2048, "cpus": 2}})

    def fake_docker(*args, timeout=60):
        seen["argv"].append(list(args))
        return (rc, "" if rc == 0 else "Error: No such container")

    with mock.patch.object(agent, "docker", fake_docker), \
         mock.patch.object(agent, "load_instances", lambda: dict(inst)), \
         mock.patch.object(agent, "save_instances",
                           lambda d: seen.update(saved=dict(d))), \
         mock.patch.object(agent, "invalidate_agent_status_cache",
                           lambda n: seen["invalidated"].append(n)):
        ok, msg = agent.resize_instance_linux(name, memory_mb, cpus)
    return ok, msg, seen


ok, msg, seen = drive_agent_resize("rbx1", 1024, 1)
check("F1 agent happy path", ok is True and msg == "resized", repr((ok, msg)))
check("F2 agent docker argv is list-form (no shell)",
      seen["argv"] == [["update", "--memory", "1024m", "--cpus", "1", "wd-rbx1"]],
      repr(seen["argv"]))
check("F3 agent instances.json updated",
      seen["saved"] and seen["saved"]["rbx1"]["memory_mb"] == 1024
      and seen["saved"]["rbx1"]["cpus"] == 1, repr(seen["saved"]))
check("F4 agent status cache invalidated", seen["invalidated"] == ["rbx1"],
      repr(seen["invalidated"]))

ok, msg, seen = drive_agent_resize("rbx1", 4096, None)
check("F5 memory-only argv", seen["argv"] == [["update", "--memory", "4096m", "wd-rbx1"]],
      repr(seen["argv"]))

ok, msg, seen = drive_agent_resize("rbx1", None, 4)
check("F6 cpus-only argv", seen["argv"] == [["update", "--cpus", "4", "wd-rbx1"]],
      repr(seen["argv"]))

ok, msg, seen = drive_agent_resize("rbx1", 1024, 1, rc=1)
check("F7 agent docker failure -> (False, msg)", ok is False and "docker update failed" in msg,
      repr((ok, msg)))
check("F8 agent failure: json untouched", seen["saved"] is None, repr(seen["saved"]))

ok, msg, seen = drive_agent_resize("a%2f..%2f..", 1024, 1)
check("F9 hostile name refused, docker never called",
      ok is False and msg == "invalid name" and seen["argv"] == [],
      repr((ok, msg, seen["argv"])))

# ---------- G. agent resize_usage_ok ----------------------------------------
with mock.patch.object(agent, "instance_resources_all",
                       lambda: {"rbx1": {"mem_used_mb": 1500.0, "cpu_pct": 5}}):
    ok, err = agent.resize_usage_ok("rbx1", 1024)
    check("G1 shrink below usage refused",
          ok is False and "below current usage" in (err or ""), repr((ok, err)))
    ok, err = agent.resize_usage_ok("rbx1", 2048)
    check("G2 grow proceeds", ok is True and err is None, repr((ok, err)))
    ok, err = agent.resize_usage_ok("rbx1", None)
    check("G3 cpus-only resize skips the memory guard", ok is True, repr((ok, err)))
with mock.patch.object(agent, "instance_resources_all", lambda: {}):
    ok, err = agent.resize_usage_ok("rbx1", 1024)
    check("G4 unknown usage fails open", ok is True, repr((ok, err)))
with mock.patch.object(agent, "instance_resources_all",
                       side_effect=RuntimeError("boom")):
    ok, err = agent.resize_usage_ok("rbx1", 1024)
    check("G5 raising stats never raises the guard", ok is True, repr((ok, err)))

# ---------- H. agent route contract -----------------------------------------
ASRC = open(os.path.join(os.path.dirname(__file__), "..", "..", "wing",
                         "agent.py")).read()
check("H1 agent resize route regex in do_POST",
      're.fullmatch(r"/api/agent/instances/([^/]+)/resize", path)' in ASRC)
_rz = ASRC.index('"/api/agent/instances/([^/]+)/resize"')
_gate = ASRC.index("if not valid_agent_instance_name(name):", _rz)
check("H2 agent route gates name -> 404",
      _gate < ASRC.index("require_linux()", _rz) and _gate - _rz < 400)
check("H3 agent route 501s on Windows", "require_linux()" in ASRC[_rz:_rz + 2000])
check("H4 agent bounds match manager (1024-32768 / 0.5-32)",
      "not (1024 <= memory_mb <= 32768)" in ASRC[_rz:_rz + 2000]
      and "not (0.5 <= cpus <= 32)" in ASRC[_rz:_rz + 2000])
check("H5 agent route calls the usage guard before docker",
      ASRC.index("resize_usage_ok(name, memory_mb)", _rz) <
      ASRC.index("resize_instance_linux(name, memory_mb, cpus)", _rz))

# ---------- I. dashboard contract -------------------------------------------
HTML = open(os.path.join(os.path.dirname(__file__), "..", "..", "manager",
                         "static", "index.html")).read()
check("I1 card row has the resize button",
      'data-act="resize"' in HTML and ">Recursos</button>" in HTML)
check("I2 resize button is a ghost button next to Eliminar",
      HTML.index('data-act="resize"') < HTML.index('data-act="del"'))
check("I3 resizeModal exists with all fields",
      all(x in HTML for x in ('id="resizeModal"', 'id="rzName"', 'id="rzMem"',
                              'id="rzCpus"', 'id="btnDoResize"',
                              'id="btnCancelResize"', 'id="rzErr"')))
check("I4 modal copy is Spanish, no emojis in the new strings",
      "Se aplican en vivo, sin reiniciar" in HTML
      and "Recursos —" in HTML)
check("I5 act() routes resize to openResize(inst)",
      "if (a==='resize') return openResize(inst);" in HTML)
check("I6 btnDoResize POSTs to the resize route",
      "/instances/${rzInst.name}/resize" in HTML
      and "{method:'POST', body: JSON.stringify(body)}" in HTML)

# Drive the marked instance-resize block in node with a stubbed DOM.
_blk = HTML.split("// == instance-resize begin ==")[1].split(
    "// == instance-resize end ==")[0]
_node_harness = r"""
const calls = {api: [], refresh: 0};
const els = {};
function $(id){
  if (!els[id]) els[id] = {textContent: "", value: "",
    classList: {add(c){ this._c = (this._c||[]).concat([c]); },
                remove(c){ this._c = (this._c||[]).filter(x=>x!==c); },
                has(c){ return (this._c||[]).indexOf(c) >= 0; }},
    onclick: null};
  return els[id];
}
let token = "t";
function api(path, opts){ calls.api.push([path, opts]); return Promise.resolve({ok:true}); }
function refresh(){ calls.refresh++; }
function openViewer(){}
function confirm(){ return true; }
function alert(){}
const setTimeout = (fn) => 0;
""" + _blk + r"""
(async () => {
  const out = {};
  // open the modal for a 2048MB/2CPU instance
  openResize({name: "rbx1", memory_mb: 2048, cpus: 2});
  out.modalOpen = $("resizeModal").classList.has("open");
  out.prefillMem = $("rzMem").value; out.prefillCpu = $("rzCpus").value;
  out.curText = $("rzCur").textContent; out.nameText = $("rzName").textContent;
  // apply a new size
  $("rzMem").value = "1024"; $("rzCpus").value = "1";
  await $("btnDoResize").onclick();
  out.posted = calls.api.slice();
  out.modalClosed = !$("resizeModal").classList.has("open");
  // cancel reopens then closes
  openResize({name: "rbx1", memory_mb: 2048, cpus: 2});
  $("btnCancelResize").onclick();
  out.cancelClosed = !$("resizeModal").classList.has("open");
  // empty inputs -> inline error, no POST
  calls.api.length = 0;
  openResize({name: "rbx1", memory_mb: 2048, cpus: 2});
  $("rzMem").value = ""; $("rzCpus").value = "";
  await $("btnDoResize").onclick();
  out.noPost = calls.api.length === 0;
  out.errText = $("rzErr").textContent;
  console.log("DRIVE " + JSON.stringify(out));
})();
"""
_tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
_tmp.write(_node_harness)
_tmp.close()
try:
    r = subprocess.run(["node", _tmp.name], capture_output=True, text=True,
                       timeout=30)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("DRIVE ")]
    drove = json.loads(line[0][6:]) if line else {}
except Exception as e:
    drove = {"error": str(e)}
finally:
    os.unlink(_tmp.name)
check("I7 node drive: modal opens + fields prefilled",
      drove.get("modalOpen") is True and drove.get("prefillMem") == 2048
      and drove.get("prefillCpu") == 2, repr(drove))
check("I8 node drive: current-limits line rendered",
      drove.get("curText") == "2 CPU · 2048 MB"
      and drove.get("nameText") == "rbx1", repr(drove))
check("I9 node drive: apply POSTs the resize endpoint",
      drove.get("posted") == [["/api/instances/rbx1/resize",
                               {"method": "POST",
                                "body": '{"memory_mb":1024,"cpus":1}'}]],
      repr(drove.get("posted")))
check("I10 node drive: modal closes on success", drove.get("modalClosed") is True,
      repr(drove))
check("I11 node drive: cancel closes", drove.get("cancelClosed") is True, repr(drove))
check("I12 node drive: empty inputs -> inline error, no POST",
      drove.get("noPost") is True and drove.get("errText") == "Indica RAM o CPUs.",
      repr(drove))

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
