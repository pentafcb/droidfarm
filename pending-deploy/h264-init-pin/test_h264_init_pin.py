"""Unit tests for the H.264 subscriber init pin (manager/h264_stream.py).

Gap: H264Subscriber's skip-ahead eviction dropped the OLDEST queued
chunk unconditionally when a slow viewer's queue exceeded
max_queue_bytes. The oldest chunk of a fresh subscriber is the fMP4
init segment (ftyp+moov) — the one chunk the viewer cannot decode
without. A viewer whose socket stalled before its first read (the
H.264 handler thread delayed under load while the broadcaster kept
pushing 20fps fragments, >1MB queued) therefore lost its init segment:
its first read returned bare moof fragments, the dashboard's
appendBuffer() threw, and h264Fail() downgraded that viewer to MJPEG
for the rest of the session — even though the stream itself was
healthy. Mid-stream skips are benign by comparison (a P-fragment
start re-syncs at the next IDR <=1-2s away), so only the init needed
pinning.

Fix: while the init chunk is still queued, skip-ahead evicts the
second-oldest chunk instead of the oldest, and a queue holding only
the init segment evicts nothing. The pin releases on the first drain
(read() hands the init to the viewer); eviction after that is plain
oldest-first again.

Run: python3 test_h264_init_pin.py

Hermetic: only the pure-Python H264Subscriber is exercised (no
ffmpeg, no broadcaster, no network). The old eviction loop is
re-implemented inline for one regression-proof case.
"""
import collections
import importlib.util
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..", "..")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


h264 = load("init_pin_h264", os.path.join(REPO, "manager", "h264_stream.py"))

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {name} {extra}")


INIT = b"INIT-SEGMENT"
F60 = lambda tag: (tag * 60).encode()  # 60 bytes each


# --- 1. init pinned under queue pressure ----------------------------------
sub = h264.H264Subscriber(INIT, max_queue_bytes=100)
check("pin set at construction", sub._init_pinned is True)
sub._push(F60("A"))
sub._push(F60("B"))
sub._push(F60("C"))
check("pinned queue keeps init as head", sub._q[0] == INIT)
check("pinned queue keeps only newest fragment",
      list(sub._q) == [INIT, F60("C")],
      f"got {[bytes(c)[:2] for c in sub._q]}")
check("queued accounting matches surviving chunks",
      sub._queued == len(INIT) + len(F60("C")))
data = sub.read(timeout=1.0)
check("first read starts with init segment", data.startswith(INIT))
check("first read is init + newest fragment", data == INIT + F60("C"))
check("pin released after first drain", sub._init_pinned is False)

# --- 2. old eviction loop drops init (regression proof) --------------------
old_q = collections.deque([INIT])
old_queued = len(INIT)
old_max = 100
for tag in ("A", "B", "C"):
    frag = F60(tag)
    old_q.append(frag)
    old_queued += len(frag)
    while old_queued > old_max and len(old_q) > 1:
        old = old_q.popleft()  # old code: unconditional oldest-first
        old_queued -= len(old)
check("old loop drops the init segment (what the fix prevents)",
      old_q[0] != INIT, f"old head={bytes(old_q[0])[:4]}")

# --- 3. after drain, eviction is plain oldest-first again -------------------
sub2 = h264.H264Subscriber(INIT, max_queue_bytes=100)
check("drain returns init when nothing pushed", sub2.read(timeout=1.0) == INIT)
sub2._push(F60("A"))
sub2._push(F60("B"))
sub2._push(F60("C"))
check("post-drain queue drops oldest fragments",
      list(sub2._q) == [F60("C")],
      f"got {[bytes(c)[:2] for c in sub2._q]}")
check("post-drain read is newest fragment only",
      sub2.read(timeout=1.0) == F60("C"))

# --- 4. no-init subscriber: normal oldest-first eviction -------------------
sub3 = h264.H264Subscriber(b"", max_queue_bytes=100)
check("empty init leaves pin unset", sub3._init_pinned is False)
sub3._push(F60("A"))
sub3._push(F60("B"))
sub3._push(F60("C"))
check("no-init queue drops oldest fragments",
      list(sub3._q) == [F60("C")])

# --- 5. single huge fragment cannot evict the init -------------------------
sub4 = h264.H264Subscriber(INIT, max_queue_bytes=100)
sub4._push(b"H" * 500)
check("huge fragment evicted, init survives alone",
      list(sub4._q) == [INIT])
check("read after huge-fragment eviction is init only",
      sub4.read(timeout=1.0) == INIT)

# --- 6. under-max queue: order preserved, nothing evicted -------------------
sub5 = h264.H264Subscriber(INIT, max_queue_bytes=1024)
sub5._push(F60("A"))
sub5._push(F60("B"))
check("under-max queue keeps init + fragments in order",
      list(sub5._q) == [INIT, F60("A"), F60("B")])
check("under-max read joins everything",
      sub5.read(timeout=1.0) == INIT + F60("A") + F60("B"))

# --- 7. closed subscriber: push no-op, read -> None -------------------------
sub6 = h264.H264Subscriber(INIT, max_queue_bytes=100)
sub6.close()
sub6._push(F60("A"))
check("push after close leaves queue untouched",
      list(sub6._q) == [INIT])
check("read after close -> None", sub6.read(timeout=0.5) is None)

# --- 8. read timeout on empty queue -> b"" (not None, not a close) ----------
sub7 = h264.H264Subscriber(b"", max_queue_bytes=100)
check("read timeout on empty queue -> b\"\"",
      sub7.read(timeout=0.2) == b"")

# --- 9. source contracts ----------------------------------------------------
push_src = inspect.getsource(h264.H264Subscriber._push)
read_src = inspect.getsource(h264.H264Subscriber.read)
check("_push evicts second-oldest while pinned",
      "del self._q[1]" in push_src)
check("_push still evicts oldest when unpinned",
      "self._q.popleft()" in push_src)
check("read clears the pin on drain",
      "_init_pinned = False" in read_src)

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
