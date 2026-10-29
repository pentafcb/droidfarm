#!/usr/bin/env python3
"""Tests for wing agent 1.0.18 create image preflight (manager create-preflight parity).

REAL GAP: the manager fails fast on instance create when the
`waydroid-instance` image was never built on the host (2026-10-13
create-preflight: waydroid_image_present() -> actionable 500 + reservation
rollback). The wing agent's create path had no such check: on a wing node
without the image, `docker run` failed deep with a cryptic docker error
("Unable to find image ... pull access denied") — exactly the state the
new VPS was in while Penta's srt1-srt5 request was pending. Agent 1.0.18
adds sw_image_present() (True/False/None, fail-open on unknown) and fails
fast in create_instance_linux with an actionable message, before the
network/binder dance. The message flows through the existing agent
{"ok": False, "msg": ...} -> manager passthrough -> dashboard j.msg path.

This suite covers:

  A. agent.VERSION is exactly 1.0.18 + manager >=1.0.2 gate compatibility
  B. sw_image_present: present/missing (exact + case-insensitive)/
     daemon-down/empty-garbage/raising -> True/False/None/None/None
  C. inspect args contract: docker("image", "inspect", IMAGE) list-form,
     no shell interpolation of user input
  D. create path: image missing -> (False, WING_IMAGE_MISSING_MSG), no
     further docker calls, instances not saved
  E. create path: image present -> proceeds to docker run, ok True
  F. create path: image unknown (weird error) -> fails OPEN, proceeds
  G. ordering: docker-unavailable / already-exists fire BEFORE the image
     inspect (no inspect call); loop self-heal not called on missing path
  H. message contract: names the image + the build package, no
     token/password words, non-empty str
  I. route contract: create route sends {"ok": ok, "msg": msg} so the
     dashboard's j.msg rendering shows it
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def check(cond, name, detail=""):
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


agent = load("wcp_agent", AGENT)
app = load("wcp_app", APP)


class FakeDocker:
    """agent.docker stand-in. image_state drives the `image inspect` answer;
    every docker call is logged as an arg tuple."""

    def __init__(self, image_state="present"):
        self.image_state = image_state
        self.calls = []

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[:2] == ("image", "inspect"):
            if self.image_state == "present":
                return 0, '[{"Id": "sha256:deadbeef"}]'
            if self.image_state == "missing":
                return 1, "Error: No such image: waydroid-instance:latest"
            return 1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        if args[:2] == ("network", "ls"):
            return 0, agent.NETWORK + "\n"
        if args[0] == "run":
            return 0, "deadbeef\n"
        return 0, ""


def drive_create(image_state, status="missing", loop_ok=True):
    """Drive create_instance_linux with faked docker/status/binder/loop/state.
    Returns (result, fake_docker, loop_calls, save_calls)."""
    fd = FakeDocker(image_state)
    saved = {}
    out = []
    loop_calls = []
    save_calls = []
    saved["docker"] = agent.docker
    saved["status"] = agent.container_status
    saved["triplet"] = agent.sw_find_binder_triplet
    saved["loop"] = agent.sw_ensure_loop_devices
    saved["load"] = agent.load_instances
    saved["save"] = agent.save_instances
    agent.docker = fd
    agent.container_status = lambda cname: status
    agent.sw_find_binder_triplet = lambda: [
        "/dev/binderfs/binder", "/dev/binderfs/binder1",
        "/dev/binderfs/binder2"]

    def loop_fake():
        loop_calls.append(1)
        return loop_ok

    agent.sw_ensure_loop_devices = loop_fake
    agent.load_instances = lambda: {}
    agent.save_instances = lambda d: save_calls.append(d)
    try:
        out.append(agent.create_instance_linux("img1", 2, 2048, "10.20.0.21"))
    finally:
        agent.docker = saved["docker"]
        agent.container_status = saved["status"]
        agent.sw_find_binder_triplet = saved["triplet"]
        agent.sw_ensure_loop_devices = saved["loop"]
        agent.load_instances = saved["load"]
        agent.save_instances = saved["save"]
    return out[0], fd, loop_calls, save_calls


def image_only(image_state):
    """Call sw_image_present with only agent.docker faked."""
    fd = FakeDocker(image_state)
    saved = agent.docker
    agent.docker = fd
    try:
        return agent.sw_image_present(), fd
    finally:
        agent.docker = saved


print("== A. VERSION bump + manager gate ==")
check(agent.VERSION in ("1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"),
      "A1 VERSION is 1.0.18+ (exact bump now owned by wing-auth-ratelimit)",
      repr(agent.VERSION))
check(app._version_at_least("1.0.18", "1.0.2") and
      not app._version_at_least("1.0.1", "1.0.2"),
      "A2 manager >=1.0.2 gate accepts 1.0.18", "gate contract broken")

print("== B. sw_image_present units ==")
val, fd = image_only("present")
check(val is True, "B1 image present -> True", repr(val))
val, fd = image_only("missing")
check(val is False, "B2 exact 'No such image' -> False", repr(val))
check(fd.calls and fd.calls[0][:2] == ("image", "inspect"),
      "B3 called via docker('image', 'inspect', IMAGE)", repr(fd.calls))


class LowerDocker(FakeDocker):
    def __call__(self, *args, timeout=60):
        if args[:2] == ("image", "inspect"):
            self.calls.append(args)
            return 1, "error: no such image: waydroid-instance:latest"
        return super().__call__(*args, timeout=timeout)


saved = agent.docker
agent.docker = LowerDocker("missing")
try:
    val = agent.sw_image_present()
finally:
    agent.docker = saved
check(val is False, "B4 lowercase 'no such image' -> False", repr(val))


class GarbageDocker(FakeDocker):
    def __call__(self, *args, timeout=60):
        if args[:2] == ("image", "inspect"):
            self.calls.append(args)
            return 1, ""
        return super().__call__(*args, timeout=timeout)


saved = agent.docker
agent.docker = GarbageDocker("unknown")
try:
    val = agent.sw_image_present()
finally:
    agent.docker = saved
check(val is None, "B5 empty error output -> None (fail open)", repr(val))

val, fd = image_only("unknown")
check(val is None, "B6 daemon-down output -> None (fail open)", repr(val))

saved = agent.docker


def raising_docker(*args, timeout=60):
    raise RuntimeError("boom")


agent.docker = raising_docker
try:
    val = agent.sw_image_present()
    check(val is None, "B7 raising docker -> None, never raises", repr(val))
except Exception as e:  # noqa: BLE001
    bad("B7 raising docker -> None, never raises", repr(e))
finally:
    agent.docker = saved

print("== C. inspect args contract ==")
_, fd = image_only("present")
check(len(fd.calls) == 1 and fd.calls[0][0] == "image" and
      fd.calls[0][1] == "inspect" and fd.calls[0][2] == agent.IMAGE,
      "C1 single docker('image', 'inspect', IMAGE) call, list-form",
      repr(fd.calls))
src = open(AGENT).read()
m = re.search(r"def sw_image_present\(\):(.*?)(?=\ndef |\Z)", src, re.S)
body = m.group(1) if m else ""
check("shell=True" not in body, "C2 no shell=True in sw_image_present",
      "user input can never be interpolated")
check("IMAGE" in body and "name" not in body.split("IMAGE")[0][-200:],
      "C3 inspects only the IMAGE constant")

print("== D. create: image missing -> fail fast ==")
(ok1, msg), fd, loop_calls, save_calls = drive_create("missing")
check(ok1 is False, "D1 create returns False", repr((ok1, msg)))
check(msg == agent.WING_IMAGE_MISSING_MSG, "D2 message is WING_IMAGE_MISSING_MSG",
      repr(msg))
check([c for c in fd.calls if c[0] == "run"] == [],
      "D3 no docker run attempted", repr(fd.calls))
check([c for c in fd.calls if c[:2] == ("network", "create")] == [],
      "D4 no network creation", repr(fd.calls))
check(all("binder-control" not in " ".join(c) for c in fd.calls),
      "D5 no binder writes attempted", repr(fd.calls))
check(save_calls == [], "D6 instances not saved", repr(save_calls))
check(loop_calls == [], "D7 loop self-heal not called on the missing path",
      repr(loop_calls))
check(len(fd.calls) == 1, "D8 exactly one docker call (the inspect)",
      repr(fd.calls))

print("== E. create: image present -> proceeds ==")
(ok1, msg), fd, loop_calls, save_calls = drive_create("present")
check(ok1 is True, "E1 create succeeds", repr((ok1, msg)))
check(any(c[0] == "run" and "waydroid-instance:latest" in c for c in fd.calls),
      "E2 docker run with the instance image", repr(fd.calls))
check(len(save_calls) == 1, "E3 instances saved once", repr(len(save_calls)))

print("== F. create: image unknown -> fail open ==")
(ok1, msg), fd, loop_calls, save_calls = drive_create("unknown")
check(ok1 is True, "F1 create proceeds on unknown (fail open)",
      repr((ok1, msg)))
check(any(c[0] == "run" for c in fd.calls),
      "F2 docker run attempted", repr(fd.calls))

print("== G. ordering: status checks before the inspect ==")
(ok1, msg), fd, loop_calls, save_calls = drive_create("missing",
                                                     status="unknown")
check(msg == "docker unavailable", "G1 docker-unavailable fires first",
      repr(msg))
check(fd.calls == [], "G2 no image inspect when docker is down",
      repr(fd.calls))
(ok1, msg), fd, loop_calls, save_calls = drive_create("missing",
                                                     status="running")
check(msg == "already exists", "G3 already-exists fires first", repr(msg))
check(fd.calls == [], "G4 no image inspect for existing instance",
      repr(fd.calls))
# invalid name never reaches the check either
(ok1, msg) = agent.create_instance_linux("../x", 2, 2048, "10.20.0.21")
check(msg == "invalid name", "G5 invalid name rejected first", repr(msg))

print("== H. message contract ==")
m = agent.WING_IMAGE_MISSING_MSG
check(isinstance(m, str) and len(m) > 40, "H1 non-empty string message",
      repr(m))
check("waydroid-instance" in m, "H2 names the image", repr(m))
check("waydroid-image-build" in m, "H3 names the build package (actionable)",
      repr(m))
check("token" not in m.lower() and "password" not in m.lower(),
      "H4 no credential words in the message", repr(m))

print("== I. route contract ==")
check(re.search(r'"/api/agent/instances".*?\{"ok": ok, "msg": msg\}',
                src, re.S) is not None,
      "I1 create route sends {ok, msg} (dashboard renders j.msg)")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
