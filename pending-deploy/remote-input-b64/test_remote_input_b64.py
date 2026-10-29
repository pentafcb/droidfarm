"""Unit tests for the remote b64 text-input staging fix (wing agent 1.0.38).

Bug: the agent's /input endpoint ran base64-decoded text (b64:true — used
ONLY for the remote Roblox login password) through sanitize_input_text,
which strips every character outside [a-zA-Z0-9%s.,!?@_-]. A password
containing #$&*()+=/:;<>[]{}|\\"'`~^ etc. was silently mangled, so remote
logins failed with a generic "login failed" while the same password worked
on local instances (manager types the full password via pwd.txt).

Fix: the b64 branch stages the decoded text in a 600-perm file inside the
instance's data volume and sends `input text "$(cat ...)"` through the
FIFO — the secret bytes never appear in the command line, and inside
double quotes $(...) expansion results are not re-parsed, so metachars are
inert data. The manager additionally gates remote login on agent >= 1.0.38
(old agents refuse loudly instead of mangling silently).

Run: python3 test_remote_input_b64.py
"""
import base64
import importlib.util
import os
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("rib_agent", AGENT)

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
    return tempfile.mkdtemp(prefix="rib-vol-")


# The hostile alphabet: everything sanitize_input_text strips. A realistic
# special-char-heavy password.
HOSTILE = "P@ss#w0rd$&(x);`id`'q'\"d\"~^|\\+=/:;<>[]{}"

print("== A. version bump ==")
check("VERSION is 1.0.45", agent.VERSION == "1.0.45", repr(agent.VERSION))

print("== B. path helper: name gating ==")
v = fresh_vol()
p = agent._agent_input_b64_path("srt1", vol_base=v)
check("valid name -> path under vol_base",
      p == os.path.join(v, "wd-srt1-data", "_data", "data", "media", "0",
                        "jerusalem", "input-b64.txt"), repr(p))
for bad in ("../x", "a/b", "x;rm", "", None, "s" * 33, "srt 1"):
    check(f"invalid name {bad!r} -> None",
          agent._agent_input_b64_path(bad, vol_base=v) is None)

print("== C. staging preserves hostile bytes exactly ==")
v = fresh_vol()
cmd = agent._stage_b64_input_text("srt1", HOSTILE, vol_base=v)
p = agent._agent_input_b64_path("srt1", vol_base=v)
check("staged file bytes == input bytes",
      open(p, "rb").read() == HOSTILE.encode("utf-8"))
check("file mode is 600",
      stat.S_IMODE(os.stat(p).st_mode) == 0o600)
check("no tmp file left behind",
      not os.path.exists(p + ".tmp"))
check("cmd is the fixed cat form",
      cmd == 'input text "$(cat /data/media/0/jerusalem/input-b64.txt)"',
      repr(cmd))
check("secret bytes never appear in the FIFO command",
      HOSTILE not in cmd)

print("== D. regression proof: old code would have mangled it ==")
mangled = agent.sanitize_input_text(HOSTILE)
check("sanitize strips hostile chars (the old b64 behavior)",
      mangled != HOSTILE and "#" not in mangled and "$" not in mangled
      and "&" not in mangled and "(" not in mangled,
      repr(mangled))
check("staged file keeps what sanitize destroys",
      open(p, "rb").read().decode() != mangled)

print("== E. input-text eval safety of the staged command ==")
# The FIFO is eval'd by sh in the container. Simulate: the command must
# parse as `input text` with ONE argument, and no metacharacter in the
# staged content may reach the shell (content is only ever read via
# $(cat) inside double quotes — expansion results are not re-parsed).
# Structural check: exactly `input text "<dquoted>"` with no unquoted
# metachars outside the quotes.
body = cmd[len("input text "):]
check("argument is fully double-quoted", body.startswith('"') and body.endswith('"'))
inner = body[1:-1]
check("inner is a single $(cat <fixed-path>) substitution",
      inner == "$(cat /data/media/0/jerusalem/input-b64.txt)", repr(inner))

print("== F. bounds and failure modes ==")
v = fresh_vol()
check("empty string -> None", agent._stage_b64_input_text("srt1", "", vol_base=v) is None)
check("None -> None", agent._stage_b64_input_text("srt1", None, vol_base=v) is None)
check("invalid name -> None",
      agent._stage_b64_input_text("../evil", HOSTILE, vol_base=v) is None)
long = "x" * 500
cmd = agent._stage_b64_input_text("srt1", long, vol_base=v)
p = agent._agent_input_b64_path("srt1", vol_base=v)
check("200-char cap enforced on staged file",
      cmd is not None and len(open(p, "rb").read()) == 200)
# Unwritable vol_base: point vol_base at a FILE, so makedirs fails.
fd, fpath = tempfile.mkstemp(prefix="rib-file-")
os.close(fd)
check("unwritable vol_base -> None (handler will 500, not mangle)",
      agent._stage_b64_input_text("srt1", HOSTILE, vol_base=fpath) is None)
os.remove(fpath)

print("== G. delayed delete ==")
v = fresh_vol()
old_ttl = agent._B64_TEXT_TTL_S
agent._B64_TEXT_TTL_S = 0.3
try:
    agent._stage_b64_input_text("srt1", HOSTILE, vol_base=v)
    p = agent._agent_input_b64_path("srt1", vol_base=v)
    check("file exists before ttl", os.path.exists(p))
    time.sleep(0.8)
    check("file deleted after ttl", not os.path.exists(p))
    # Newer stage is never removed early by an older deleter: simulate a
    # newer stage by overwriting the file directly (no live second deleter
    # to race with), then let the first deleter fire and confirm the file
    # survives because its content no longer matches.
    agent._stage_b64_input_text("srt1", "FIRST", vol_base=v)
    p = agent._agent_input_b64_path("srt1", vol_base=v)
    time.sleep(0.1)
    with open(p, "w") as f:
        f.write("SECOND")
    time.sleep(0.4)  # first deleter fires; content mismatch -> keeps file
    check("older deleter does not remove newer stage",
          os.path.exists(p) and open(p).read() == "SECOND")
finally:
    agent._B64_TEXT_TTL_S = old_ttl

print("== H. _agent_text_cmd helper: branch contracts ==")
v = fresh_vol()
# b64:true with hostile password -> staged, 200
b64 = base64.b64encode(HOSTILE.encode()).decode()
# NOTE: helper uses the prod vol_base; drive staging directly instead and
# assert the helper's wiring via source contract below. Here: plain path.
c, code, err = agent._agent_text_cmd("srt1", {"type": "text", "text": "hello world"})
check("plain text -> sanitized cmd, 200",
      code == 200 and c == "input text hello%sworld", repr((c, code)))
c, code, err = agent._agent_text_cmd("srt1", {"type": "text", "text": "   "})
check("plain whitespace-only keeps pre-existing contract (spaces -> %s)",
      code == 200 and c == "input text %s%s%s", repr((c, code)))
c, code, err = agent._agent_text_cmd("srt1", {"type": "text", "text": "!!!not-b64!!!", "b64": True})
check("b64:true garbage -> 400 invalid input",
      code == 400 and c is None and err == "invalid input", repr((code, err)))
c, code, err = agent._agent_text_cmd("srt1", {"type": "text", "text": "", "b64": True})
check("b64:true empty -> 400", code == 400 and c is None)
c, code, err = agent._agent_text_cmd("srt1", {"type": "text"})
check("missing text -> 400", code == 400 and c is None)
c, code, err = agent._agent_text_cmd("srt1", {"type": "text", "text": b64, "b64": True})
check("b64:true hostile password stages on prod vol base (or 500 if no volume)",
      (code == 200 and c == 'input text "$(cat /data/media/0/jerusalem/input-b64.txt)"')
      or (code == 500 and err == "could not stage text input"),
      repr((code, err)))
c, code, err = agent._agent_text_cmd("../evil", {"type": "text", "text": b64, "b64": True})
check("b64:true invalid name -> 500 (never stages outside the volume)",
      code == 500 and c is None, repr((code, err)))
c, code, err = agent._agent_text_cmd("srt1", None)
check("garbage data -> 400, never raises", code == 400 and c is None)

print("== I. handler wiring: b64 branch uses the helper, no sanitize ==")
import re
src = open(AGENT).read()
m = re.search(r'elif itype == "text":(.*?)(?=\n        m = re\.fullmatch)', src, re.S)
check("text branch found in handler", bool(m))
branch = m.group(1) if m else ""
check("branch delegates to _agent_text_cmd",
      "_agent_text_cmd(name, data)" in branch, branch[:200])
check("branch no longer calls sanitize_input_text on the b64 path",
      "sanitize_input_text" not in branch, branch[:200])
check("plain-text path still sanitizes (in helper)",
      "sanitize_input_text(raw)" in src)

print("== J. manager: remote-login gate is 1.0.38 ==")
app_src = open(APP).read()
check("gate requires 1.0.38",
      '_version_at_least(ver, "1.0.38")' in app_src)
check("gate message names 1.0.38",
      "needs 1.0.38+" in app_src)
check("old 1.0.2 gate gone",
      '_version_at_least(ver, "1.0.2")' not in app_src)
check("manager still sends b64:true for the remote password",
      '"b64": True' in app_src)

print("== K. exploit regression proof ==")
# A password crafted to break out of the old inline interpolation can no
# longer reach a shell: the staged command carries zero secret bytes.
evil = 'x"; rm -rf /; echo "'
v = fresh_vol()
cmd = agent._stage_b64_input_text("srt1", evil, vol_base=v)
check("hostile password stages byte-identical",
      open(agent._agent_input_b64_path("srt1", vol_base=v), "rb").read() == evil.encode())
check("staged command is still the fixed cat form",
      cmd == 'input text "$(cat /data/media/0/jerusalem/input-b64.txt)"')

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
