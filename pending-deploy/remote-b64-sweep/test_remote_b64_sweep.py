#!/usr/bin/env python3
"""Unit tests for the remote b64 staging sweep (wing agent, no version bump).

Gap: remote-input-b64 stages a decoded /input b64 payload — in practice a
Roblox login password — in the instance volume as input-b64.txt (600,
atomic tmp+rename), deleted ~60s later by a daemon thread. The thread dies
with the agent process, so an agent restart inside the 60s window (crash,
self-update, systemd restart, host reboot) leaves the staged secret in the
volume forever, readable from inside Android and from the host volume path
— the same leak class the manager's login-pwd-cleanup closed locally. A
thread-start failure leaves it behind too (the handler 500s, but the file
is already staged), and a crash between the tmp write and os.replace
leaves input-b64.txt.tmp behind.

Fix (wing/agent.py only, no manager gate depends on it, self-update is
sha-driven, so VERSION stays 1.0.38 like remote-launch-fifo):
  * sweep_stale_b64_input_text() removes input-b64.txt + input-b64.txt.tmp
    per instance (name-gated paths, never raises, returns the count).
  * main() sweeps unconditionally at startup, before any thread starts
    (no live stage can exist yet — the server is not bound).
  * selfwatch_check_instance() sweeps files older than 2x the TTL each
    pass (a live stage is deleted by its own thread at 60s, so anything
    older is orphaned; the FIFO eval already happened, so the age gate
    cannot break a live typing). Alert-only: never affects `healthy`.

Run: python3 test_remote_b64_sweep.py
"""
import importlib.util
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("rbs_agent", AGENT)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


def fresh_vol():
    return tempfile.mkdtemp(prefix="rbs-vol-")


def jer(vol, name="srt1"):
    d = os.path.join(vol, "wd-%s-data" % name, "_data", "data", "media",
                     "0", "jerusalem")
    os.makedirs(d, exist_ok=True)
    return d


def stage(vol, name="srt1", text="P@ss#w0rd$&(x)", old=None, tmp=False):
    d = jer(vol, name)
    fn = "input-b64.txt.tmp" if tmp else "input-b64.txt"
    p = os.path.join(d, fn)
    with open(p, "w") as f:
        f.write(text)
    if old is not None:
        ts = time.time() - old
        os.utime(p, (ts, ts))
    return p


def read_log(st):
    try:
        return open(os.path.join(st, "selfwatch.log")).read()
    except Exception:
        return ""


print("== A. version unchanged + sweep exists ==")
check("VERSION is 1.0.44",
      agent.VERSION == "1.0.45", repr(agent.VERSION))
check("sweep_stale_b64_input_text exists",
      callable(getattr(agent, "sweep_stale_b64_input_text", None)))
check("_B64_TEXT_TTL_S still 60", agent._B64_TEXT_TTL_S == 60,
      repr(agent._B64_TEXT_TTL_S))

print("== B. path gating ==")
v = fresh_vol()
p = agent._agent_input_b64_path("srt1", vol_base=v)
check("valid name -> path under vol_base",
      p == os.path.join(v, "wd-srt1-data", "_data", "data", "media", "0",
                        "jerusalem", "input-b64.txt"), repr(p))
for badname in ("../evil", "a/b", "", "srt 1", "a" * 33, "srt1;rm"):
    check("traversal/metachar name refused: %r" % badname,
          agent._agent_input_b64_path(badname, vol_base=v) is None)

print("== C. startup sweep (unconditional) ==")
v = fresh_vol()
p = stage(v, text="secret-pw")
pt = stage(v, tmp=True, text="secret-pw-tmp")
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v)
check("staged file + tmp both removed, count 2",
      n == 2 and not os.path.exists(p) and not os.path.exists(pt), repr(n))
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v)
check("second sweep is a no-op (count 0)", n == 0, repr(n))
v = fresh_vol()
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v)
check("missing instance dir -> 0, never raises", n == 0, repr(n))
v = fresh_vol()
d = jer(v)
for fn in ("live.png", "stream_wanted", "booted", "screencap-loop.sh"):
    open(os.path.join(d, fn), "w").write("x")
stage(v)
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v)
check("unrelated volume files untouched",
      n == 1 and all(os.path.exists(os.path.join(d, fn)) for fn in
                     ("live.png", "stream_wanted", "booted",
                      "screencap-loop.sh")), repr(n))
v = fresh_vol()
stage(v, name="srt1")
n = agent.sweep_stale_b64_input_text(names=["../evil", "a/b", ""],
                                     vol_base=v)
check("traversal names skipped, nothing removed", n == 0, repr(n))
check("legit file still present after traversal sweep",
      os.path.exists(agent._agent_input_b64_path("srt1", vol_base=v)))
v = fresh_vol()
stage(v)
orig_load = agent.load_instances
agent.load_instances = lambda: {"srt1": {}}
try:
    n = agent.sweep_stale_b64_input_text(vol_base=v)
finally:
    agent.load_instances = orig_load
check("names=None reads load_instances() keys", n == 1, repr(n))
v = fresh_vol()
stage(v)
n = agent.sweep_stale_b64_input_text(names={"srt1": {"provider": "x"}},
                                     vol_base=v)
check("dict instances contribute keys", n == 1, repr(n))
n = agent.sweep_stale_b64_input_text(names=42, vol_base=v)
check("non-iterable instances -> 0, never raises", n == 0, repr(n))
v = fresh_vol()
d = jer(v)
os.makedirs(os.path.join(d, "input-b64.txt"), exist_ok=True)
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v)
check("is-a-directory staging entry -> 0, never raises, dir survives",
      n == 0 and os.path.isdir(os.path.join(d, "input-b64.txt")), repr(n))

print("== D. age-gated sweep (selfwatch semantics) ==")
v = fresh_vol()
p = stage(v)  # fresh: seconds old
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v,
                                     max_age_s=120)
check("fresh file survives 120s age gate",
      n == 0 and os.path.exists(p), repr(n))
v = fresh_vol()
p = stage(v, old=3600)
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v,
                                     max_age_s=120)
check("3600s-old file removed by 120s age gate",
      n == 1 and not os.path.exists(p), repr(n))
v = fresh_vol()
pt = stage(v, tmp=True)  # fresh tmp
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v,
                                     max_age_s=120)
check("fresh tmp survives age gate",
      n == 0 and os.path.exists(pt), repr(n))
v = fresh_vol()
pt = stage(v, tmp=True, old=3600)
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v,
                                     max_age_s=120)
check("old tmp removed by age gate",
      n == 1 and not os.path.exists(pt), repr(n))
v = fresh_vol()
p = stage(v, old=3600)
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v,
                                     max_age_s=24 * 3600)
check("old file survives a larger age gate",
      n == 0 and os.path.exists(p), repr(n))
n = agent.sweep_stale_b64_input_text(names=["missing"], vol_base=v,
                                     max_age_s=120)
check("missing instance under age gate -> 0", n == 0, repr(n))

print("== E. selfwatch integration (fake docker, healthy pass) ==")


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.containers = {}
        self.helpers = {}
        self.session_alive = True

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "ps":
            return 0, "\n".join(self.containers.keys())
        if args[0] == "exec":
            cname = args[1]
            if "getprop" in args:
                return (0, "1\n") if self.session_alive else (1, "")
            if "pgrep" in args:
                return (0, "4242\n") if self.helpers.get((cname, args[-1])) else (1, "")
            if "pkill" in args:
                self.helpers[(cname, args[-1])] = False
                return 0, ""
            return 0, ""
        if args[0] == "start":
            self.containers[args[1]] = True
            return 0, ""
        return 0, ""


def sw_env(stale_b64):
    st = tempfile.mkdtemp(prefix="rbs-st-")
    vol = fresh_vol()
    fake = FakeDocker()
    fake.containers["wd-srt1"] = True
    for s in ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"):
        fake.helpers[("wd-srt1", s)] = True
    d = jer(vol)
    open(os.path.join(d, "booted"), "w").write("1\n")
    for s in ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"):
        open(os.path.join(d, s), "w").write("#!/bin/sh\n")
    png = os.path.join(d, "live.png")
    open(png, "w").write("x")
    p = stage(vol, old=3600 if stale_b64 else None)
    return st, vol, fake, p


orig_docker = agent.docker
st, vol, fake, p = sw_env(stale_b64=True)
agent.docker = fake
try:
    healthy = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    agent.docker = orig_docker
check("stale b64 removed by a selfwatch pass", not os.path.exists(p))
check("selfwatch pass still reports healthy (sweep is alert-only)",
      healthy is True, repr(healthy))
check("selfwatch log records the stale staging removal",
      "stale b64 input staging removed" in read_log(st), read_log(st)[:200])
st, vol, fake, p = sw_env(stale_b64=False)
agent.docker = fake
try:
    healthy = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    agent.docker = orig_docker
check("fresh b64 survives a selfwatch pass", os.path.exists(p))
check("fresh-b64 pass still healthy", healthy is True, repr(healthy))
check("no stale-removal log line on fresh-b64 pass",
      "stale b64 input staging removed" not in read_log(st))

print("== F. crash-window proof + staging regression ==")
v = fresh_vol()
p = stage(v)
check("post-crash state (file, no thread) persists until swept",
      os.path.exists(p))
n = agent.sweep_stale_b64_input_text(names=["srt1"], vol_base=v)
check("startup sweep removes the post-crash file",
      n == 1 and not os.path.exists(p), repr(n))
v = fresh_vol()
old_ttl = agent._B64_TEXT_TTL_S
agent._B64_TEXT_TTL_S = 0.3
try:
    cmd = agent._stage_b64_input_text("srt1", "P@ss#w0rd$&(x)", vol_base=v)
finally:
    pass
check("staging still works (cat form, no secret in command)",
      cmd == 'input text "$(cat /data/media/0/jerusalem/input-b64.txt)"',
      repr(cmd))
check("staged bytes are byte-identical to the secret",
      open(agent._agent_input_b64_path("srt1", vol_base=v), "rb").read()
      == "P@ss#w0rd$&(x)".encode())
time.sleep(0.7)
check("cleanup thread still deletes at TTL",
      not os.path.exists(agent._agent_input_b64_path("srt1", vol_base=v)))
agent._B64_TEXT_TTL_S = old_ttl

print("== G. source contracts ==")
src = open(AGENT).read()
i_roll = src.find("maybe_rollback_agent_update()")
i_sweep = src.find("sweep_stale_b64_input_text()", i_roll)
i_serve = src.find("serve_forever", i_sweep)
check("main() sweeps after rollback guard, before serve_forever",
      0 <= i_roll < i_sweep < i_serve, "%s %s %s" % (i_roll, i_sweep, i_serve))
i_sw = src.find("def selfwatch_check_instance")
i_sw_sweep = src.find("sweep_stale_b64_input_text", i_sw)
i_sw_next = src.find("# 1. container running?", i_sw)
check("selfwatch_check_instance sweeps before the container check",
      0 <= i_sw < i_sw_sweep < i_sw_next)
check("selfwatch sweep uses 2x TTL age gate",
      "max_age_s=2 * _B64_TEXT_TTL_S" in src[i_sw:i_sw_next + 2000])
check("selfwatch sweep logs via _sw_log, never touches healthy",
      "_sw_log(st_dir, \"%s stale b64 input staging removed" in src)
check("sweep covers the tmp sibling",
      'path + ".tmp"' in src)

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
