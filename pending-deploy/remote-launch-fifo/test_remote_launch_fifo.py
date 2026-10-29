"""Unit tests for the remote launch-roblox FIFO fix (wing/agent.py).

Gap: the wing agent's launch_roblox_linux() wrote the 5-command launch
sequence (HOME, drawer swipe, Roblox tap) as FIVE separate os.write()
calls on one FIFO fd, while the input daemon does `read cmd < $FIFO`
(one line per FIFO open, reopening after every line). Once the writer
closed, the lines after the first were discarded: remote "Launch
Roblox" only ever executed HOME and never opened the drawer or tapped
the icon. The manager's LOCAL path was fixed to one compound write in
October (the FIFO multi-command delivery fix); the agent's REMOTE path
was never fixed — a local/remote parity gap, same class as
remote-input-b64 (remote login passwords mangled by sanitize).

Fix: launch_roblox_linux() sends ONE compound write ("; ".join(cmds)),
and the sequence now matches the manager's verified local sequence
(swipe settle sleep 2, not sleep 1).

Hermetic: imports the real wing/agent.py; delivery is proven against a
REAL FIFO with a daemon-faithful reader thread (open per line, an eval
delay between iterations, exactly like `while true; do read cmd <
$FIFO; eval "$cmd"; done`). No docker, no network, no disk writes
outside a temp dir.

Run: python3 test_remote_launch_fifo.py
"""
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("rlf_agent", os.path.join(REPO, "wing", "agent.py"))

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


class DaemonReader(threading.Thread):
    """Faithful model of boot-complete.sh's input daemon:

    while true; do if read cmd < $FIFO; then eval "$cmd"; fi; done
    Each iteration opens the FIFO fresh, reads ONE line, closes, then
    evals (modelled by EVAL_DELAY) before reopening.
    """

    EVAL_DELAY = 0.05

    def __init__(self, fifo):
        super().__init__(daemon=True)
        self.fifo = fifo
        self.lines = []
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                fd = os.open(self.fifo, os.O_RDONLY)  # blocks until writer
            except OSError:
                break
            try:
                with os.fdopen(fd, "r") as f:
                    line = f.readline()
                if line:
                    self.lines.append(line.rstrip("\n"))
                    time.sleep(self.EVAL_DELAY)
            except OSError:
                break

    def stop(self):
        self._stop_evt.set()
        # Unblock a reader parked in open(): dummy writer open+close.
        try:
            fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
        self.join(timeout=5)


OLD_CMDS = ["input keyevent 3", "sleep 1", "input swipe 512 400 512 100",
            "sleep 1", "input tap 129 424"]
EXPECTED_COMPOUND = ("input keyevent 3; sleep 1; "
                     "input swipe 512 400 512 100; sleep 2; "
                     "input tap 129 424")

tmp = tempfile.mkdtemp(prefix="rlf-")
fifo = os.path.join(tmp, "input.fifo")
os.mkfifo(fifo)
orig_find_fifo = agent._find_fifo
agent._find_fifo = lambda cname: fifo  # noqa: E731
# 1.0.41 focus gate: launch_roblox_linux() probes dumpsys window first
# and taps only when unfocused. The real docker binary must never run
# in this hermetic test, so answer the probe as "unfocused" (launcher
# in focus) for every launch below; the gate itself (focused/unknown)
# is covered by the launch-focus-gate suite (t143).
orig_docker = agent.docker
UNFOCUSED_DUMP = ("  mCurrentFocus=Window{abc u0 com.android.launcher/"
                  "com.android.launcher.Launcher}\n")


def fake_docker(*args, timeout=None):
    return 0, UNFOCUSED_DUMP


agent.docker = fake_docker

try:
    # ---- A. regression proof: old-style 5-write delivery loses lines ----
    reader = DaemonReader(fifo)
    reader.start()
    time.sleep(0.05)  # let the reader park in open(), like the live daemon
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        # The old function wrapped this loop in try/except OSError, so a
        # mid-loop EPIPE (daemon closed its read end after line 1 while
        # the writer was still writing) surfaced as False, not a crash —
        # the launch still failed. Model that faithfully.
        try:
            for c in OLD_CMDS:
                os.write(fd, (c + "\n").encode())
        except OSError:
            pass
    finally:
        os.close(fd)
    time.sleep(0.4)
    check("old-style 5-write delivery: daemon receives ONLY the first line",
          reader.lines == ["input keyevent 3"])
    check("old-style delivery: swipe + tap never arrive",
          not any("swipe" in l or "tap 129" in l for l in reader.lines))
    reader.stop()

    # ---- B. new code: one compound write, full sequence arrives ----
    reader = DaemonReader(fifo)
    reader.start()
    time.sleep(0.05)
    ok = agent.launch_roblox_linux("rbx1")
    time.sleep(0.4)
    check("launch_roblox_linux returns True with a listening daemon", ok is True)
    check("new delivery: daemon receives exactly one line",
          len(reader.lines) == 1)
    check("new delivery: the one line is the full compound sequence",
          reader.lines == [EXPECTED_COMPOUND])
    check("new delivery: compound holds all 5 commands in order",
          reader.lines and reader.lines[0].split("; ") == [
              "input keyevent 3", "sleep 1", "input swipe 512 400 512 100",
              "sleep 2", "input tap 129 424"])
    reader.stop()

    # ---- C. gates and failure modes ----
    check("traversal name refused", agent.launch_roblox_linux("../evil") is False)
    check("space name refused", agent.launch_roblox_linux("a b") is False)
    check("empty name refused", agent.launch_roblox_linux("") is False)
    agent._find_fifo = lambda cname: os.path.join(tmp, "no-such.fifo")
    check("missing FIFO -> False, never raises",
          agent.launch_roblox_linux("rbx1") is False)
    agent._find_fifo = lambda cname: fifo
    # No reader parked: O_NONBLOCK open fails (ENXIO) -> False, like input_linux.
    check("no listening daemon -> False (no hang)",
          agent.launch_roblox_linux("rbx1") is False)

    # ---- D. source contracts ----
    import inspect
    src = inspect.getsource(agent.launch_roblox_linux)
    check("source: single compound write via '; '.join",
          '"; ".join(cmds)' in src)
    check("source: no per-command write loop remains",
          "for c in cmds:" not in src)
    check("compound fits in one atomic PIPE_BUF write",
          len(EXPECTED_COMPOUND) < 4096)

    # ---- E. manager parity: local launch sequence == agent sequence ----
    with open(os.path.join(REPO, "manager", "app.py")) as f:
        app_src = f.read()
    m = re.search(r"return _send_input_cmds\(name, \[(.*?)\]\)",
                  app_src, re.S)
    mgr_seq = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    m2 = re.search(r"cmds = \[(.*?)\]", src, re.S)
    ag_seq = re.findall(r'"([^"]+)"', m2.group(1)) if m2 else []
    check("manager local launch sequence parses", len(mgr_seq) == 5)
    check("agent launch sequence parses", len(ag_seq) == 5)
    check("agent sequence == manager verified local sequence",
          ag_seq == mgr_seq)
    check("compound == '; '.join(manager sequence)",
          EXPECTED_COMPOUND == "; ".join(mgr_seq))

    # ---- F. version bump to 1.0.42 (missing-frame-guard) ----
    check("agent VERSION is 1.0.44",
          agent.VERSION == "1.0.45")
finally:
    agent._find_fifo = orig_find_fifo
    agent.docker = orig_docker
    shutil.rmtree(tmp, ignore_errors=True)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
