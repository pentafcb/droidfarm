"""H.264-over-WebSocket (fMP4 + MSE) stream encoder for Jerusalem 1.0.

Feeds raw RGB frames into an ffmpeg subprocess emitting fragmented MP4.
Browsers play it via Media Source Extensions — real video compression through
the Cloudflare tunnel, no UDP/ICE needed (unlike WebRTC).

Verified prototype (2026-10-06, ffmpeg 8.1.2):
- incremental output: first bytes (ftyp+moov init) ~100-300ms after start
- codec string avc1.42C015 (Baseline — universally MSE-compatible)
- bandwidth vs MJPEG q60: 13.2x smaller on static screens (identical frames),
  ~1.1x on worst-case full-motion noise; typical gameplay 1.5-3x
- 20fps sustained end-to-end on modest CPU (libx264 ultrafast, 480x284)

IMPORTANT: input must be rawvideo. image2pipe+mjpeg stalls the ffmpeg
pipeline until stdin EOF on the tested build; rawvideo emits from ~60ms.
Callers PIL-decode screenshots first (manager already has PIL).
"""
import select
import shutil
import subprocess
import threading
import time

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
HAS_FFMPEG = bool(shutil.which("ffmpeg"))


class H264Encoder:
    """Encode raw RGB24 frames to fMP4 (H.264) fragments.

    width/height should be even (odd dims are padded up automatically).
    One instance per viewer connection; call close() when the socket ends.
    """

    def __init__(self, width=480, height=284, fps=20, gop=20):
        self.width = width + (width % 2)
        self.height = height + (height % 2)
        self.fps = fps
        self.gop = gop
        self.proc = None
        self._stderr_tail = b""

    def start(self):
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            # Wall-clock input timestamps: when the broadcaster skips
            # byte-identical frames (static screen), the encoded stream
            # keeps correct PTS with real gaps instead of drifting behind
            # wall-clock time (which would add permanent latency to the
            # "live" view on every static period).
            "-use_wallclock_as_timestamps", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{self.width}x{self.height}",
            "-framerate", str(self.fps),
            "-i", "pipe:0",
            "-vcodec", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-pix_fmt", "yuv420p",
            "-g", str(self.gop), "-keyint_min", str(self.gop),
            # A keyframe at least every 2s of media time: with frag_keyframe
            # the muxer only emits a fragment on a keyframe, so without this
            # the broadcaster's static-heartbeat encodes (P-frames) would
            # buffer unemitted for up to gop/heartbeat seconds and the
            # dashboard would see no fragments for tens of seconds on a
            # static screen. During active 20fps streaming the regular
            # gop interval already keys every 1s, so this changes nothing.
            "-force_key_frames", "expr:gte(t,n_forced*2)",
            # Every fed frame is encoded with its own timestamp: the default
            # vsync would drop/duplicate frames to fit 20fps, which silently
            # eats burst-written frames once timestamps are wall-clock.
            "-fps_mode", "passthrough",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        try:
            while True:
                chunk = self.proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_tail = (self._stderr_tail + chunk)[-4096:]
        except Exception:
            pass

    def write_frame(self, rgb_bytes):
        """Feed one raw RGB24 frame (width*height*3 bytes). False if dead."""
        if not self.proc or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write(rgb_bytes)
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def read_available(self, timeout=0.0):
        """Read whatever fMP4 bytes are ready (b"" if none within timeout)."""
        out = b""
        end = time.time() + timeout
        try:
            while True:
                r, _, _ = select.select([self.proc.stdout], [], [],
                                        max(0.0, end - time.time()))
                if not r:
                    break
                # raw FileIO (bufsize=0) has no read1(); after select says
                # readable, read() returns whatever the pipe has available.
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    break
                out += chunk
                if time.time() >= end:
                    break
        except (OSError, ValueError):
            pass
        return out

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def error(self):
        return self._stderr_tail.decode(errors="replace")[-500:]

    def close(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
                p.wait(timeout=5)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared broadcaster: ONE ffmpeg encoder per instance, fanning out fMP4
# fragments to any number of viewers.
#
# Why: the first H.264 integration ran one encoder per viewer connection.
# On an 8-vCPU box also running 6 Waydroid instances, two viewers on the same
# instance meant 2x libx264 encodes + 2x screenshot fetch/JPEG-decode of the
# same image at 20fps. The broadcaster encodes and screenshots once per tick
# and hands every subscriber the same bytes, so viewer #2..N cost ~nothing.
#
# Wire protocol is unchanged (fMP4 init + fragments over WebSocket), so the
# dashboard needs no changes.
# ---------------------------------------------------------------------------
import collections  # noqa: E402


def split_mp4_boxes(buf):
    """Split raw fMP4 bytes into complete top-level boxes.

    Returns (boxes, leftover) where boxes is a list of (type, box_bytes)
    and leftover holds an incomplete trailing box (feed it more bytes).
    Handles 32-bit sizes and size==1 (64-bit largesize). A size==0 box
    ("extends to end of stream") is treated as incomplete.
    """
    boxes = []
    off = 0
    n = len(buf)
    while off + 8 <= n:
        size = int.from_bytes(buf[off:off + 4], "big")
        btype = bytes(buf[off + 4:off + 8])
        hdr = 8
        if size == 1:
            if off + 16 > n:
                break
            size = int.from_bytes(buf[off + 8:off + 16], "big")
            hdr = 16
        elif size == 0:
            break  # "to end of stream" — can't bound it, wait for more
        if size < hdr or size > 64 * 1024 * 1024:
            break  # corrupt / absurd
        if off + size > n:
            break  # incomplete box, wait for more bytes
        boxes.append((btype, bytes(buf[off:off + size])))
        off += size
    return boxes, buf[off:]


def _fragment_has_idr(fragment):
    """True if an fMP4 fragment's mdat holds an H.264 IDR NAL (type 5).

    Used to cache the most recent random-access fragment so a late
    subscriber gets instantly-decodable video even when the screen has
    been static (the broadcaster skips identical frames, so live
    fragments may be P-frames referencing a long-gone IDR).
    Best-effort, never raises: unparseable fragments simply aren't cached.
    """
    try:
        boxes, _ = split_mp4_boxes(fragment)
        for btype, bdata in boxes:
            if btype != b"mdat":
                continue
            # avcC-style length-prefixed NAL units (4-byte big-endian).
            off, end = 8, len(bdata)
            while off + 4 <= end:
                n = int.from_bytes(bdata[off:off + 4], "big")
                off += 4
                if n <= 0 or n > 4 * 1024 * 1024 or off + n > end:
                    break
                if bdata[off] & 0x1F == 5:  # coded slice of an IDR picture
                    return True
                off += n
    except Exception:
        pass
    return False


class H264Subscriber:
    """One viewer's tap into a broadcaster.

    read(timeout) returns the next chunk of bytes (b"" on timeout,
    None once closed). The first chunk is always the fMP4 init segment
    (ftyp+moov); later chunks start on fragment (moof) boundaries so the
    viewer can append them to a MediaSource buffer directly.
    Slow viewers skip ahead (oldest fragments dropped) instead of growing
    unbounded memory or stalling everyone else.

    Init pin (h264-init-pin): the init segment is the ONE chunk the
    viewer cannot decode without — MSE appendBuffer() of a fragment
    with no init segment throws, and the dashboard treats that as a
    fatal H.264 failure and downgrades the viewer to MJPEG for the rest
    of the session. The old skip-ahead dropped the oldest chunk
    unconditionally, so a viewer whose socket stalled before its first
    read (handler-thread delay under load, then >1MB of fragments
    queued) lost its init segment and was silently downgraded even
    though the stream itself was healthy. The init chunk is therefore
    pinned: while it is still queued, skip-ahead evicts the
    second-oldest chunk instead, and a queue holding only the init
    segment never evicts at all. The pin releases on the first drain
    (read() hands the init to the viewer), after which eviction is
    plain oldest-first again — a mid-stream skip then starts at a
    P-fragment at worst, and the next IDR (<=1-2s away) re-syncs the
    decoder without any downgrade.
    """

    def __init__(self, init_bytes, max_queue_bytes=1024 * 1024):
        self._lock = threading.Lock()
        self._q = collections.deque()
        self._queued = 0
        self._max = max_queue_bytes
        self._ev = threading.Event()
        self.closed = False
        # True while the init chunk is still queued (queue head, never
        # yet drained by read()); cleared on the first drain.
        self._init_pinned = False
        if init_bytes:
            self._q.append(bytes(init_bytes))
            self._queued += len(init_bytes)
            self._init_pinned = True
            self._ev.set()

    def _push(self, data):
        with self._lock:
            if self.closed:
                return
            data = bytes(data)
            self._q.append(data)
            self._queued += len(data)
            # skip-ahead: drop oldest fragments, keep the newest —
            # but never the pinned init segment (see class docstring).
            # Evicting index 1 while pinned keeps [init, newest];
            # a queue of [init] alone (len == 1) evicts nothing.
            while self._queued > self._max and len(self._q) > 1:
                if self._init_pinned:
                    old = self._q[1]
                    del self._q[1]
                else:
                    old = self._q.popleft()
                self._queued -= len(old)
            self._ev.set()

    def read(self, timeout=1.0):
        end = time.time() + timeout
        while True:
            with self._lock:
                if self.closed:
                    return None
                if self._q:
                    data = b"".join(self._q)
                    self._q.clear()
                    self._queued = 0
                    # The init segment (if it was queued) has now been
                    # handed to the viewer; the pin is spent.
                    self._init_pinned = False
                    self._ev.clear()
                    return data
            remaining = end - time.time()
            if remaining <= 0:
                return b""
            self._ev.wait(remaining)

    def close(self):
        with self._lock:
            self.closed = True
            self._ev.set()


class H264Broadcaster:
    """Owns one H264Encoder; subscribers share its fMP4 output.

    frame_source() -> (rgb_bytes, w, h) or None, called at fps cadence.
    start(timeout) primes the encoder and waits for the init segment;
    returns False if no frames arrive in time. The loop stops on its own
    when the encoder dies, when no frames arrive for frameless_timeout
    seconds (dead screencap loop / unreachable wing node would otherwise
    spin at fps forever hammering the source with an idle encoder), or
    when there are no subscribers for idle_timeout seconds.
    stop() is idempotent.

    Static-skip: a byte-identical frame adds nothing visually, so it is
    not re-encoded (the libx264 encode is the dominant per-tick cost) —
    except a heartbeat encode every static_heartbeat seconds, which keeps
    fragments flowing so the dashboard's 3s stall badge never false-fires.
    The encoder uses wall-clock input timestamps, so skipped frames leave
    correct PTS gaps instead of drifting the "live" view behind real time.
    The most recent IDR fragment is cached and pushed to late subscribers
    right after the init segment, giving instant video on static screens.
    """

    def __init__(self, frame_source, width=480, height=284, fps=20, gop=20,
                 idle_timeout=10.0, frameless_timeout=15.0,
                 max_queue_bytes=1024 * 1024, static_heartbeat=2.0):
        self._frame_source = frame_source
        self.width = width + (width % 2)
        self.height = height + (height % 2)
        self.fps = fps
        self.gop = gop
        self._idle_timeout = idle_timeout
        self._frameless_timeout = frameless_timeout
        self._max_queue_bytes = max_queue_bytes
        self._static_heartbeat = static_heartbeat
        self._enc = None
        self._enc_w = self.width
        self._enc_h = self.height
        self._init = None
        self._buf = b""      # unparsed encoder output
        self._cur = b""      # fragment being assembled (starts at moof)
        self._pre = b""      # bytes before the first moof (init candidate)
        self._subs = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_unsub = time.time()
        self._stopped = False
        self._last_encoded = None   # rgb bytes of the last encoded frame
        self._last_encode_t = 0.0   # wall time of the last encode
        self._last_idr = None       # most recent IDR-bearing fragment

    # -- lifecycle ------------------------------------------------------
    def start(self, timeout=8.0):
        """Prime encoder + wait for init segment. Returns True on success."""
        t_end = time.time() + timeout
        first = None
        while time.time() < t_end and first is None:
            try:
                first = self._frame_source()
            except Exception:
                first = None
            if first is None:
                time.sleep(0.1)
        if first is None:
            return False
        rgb, w, h = None, self.width, self.height
        try:
            rgb, w, h = first
        except (TypeError, ValueError):
            return False
        self._enc_w = w + (w % 2)
        self._enc_h = h + (h % 2)
        if (w, h) != (self._enc_w, self._enc_h):
            rgb = self._rescale(rgb, w, h, self._enc_w, self._enc_h)
            if rgb is None:
                return False
        enc = H264Encoder(width=self._enc_w, height=self._enc_h,
                          fps=self.fps, gop=self.gop)
        try:
            enc.start()
        except Exception:
            return False
        self._enc = enc
        # feed until the init segment (everything before first moof) parses
        t_end = time.time() + timeout
        while time.time() < t_end and self._init is None:
            if not enc.write_frame(rgb):
                break
            self._feed(enc.read_available(timeout=0.05))
            # Paced writes: the encoder uses wall-clock input timestamps,
            # so frames written in a tight burst would share near-identical
            # timestamps and the muxer would drop them. (The _run loop is
            # paced by its fps tick; only this priming loop needs it.)
            time.sleep(0.05)
        if self._init is None:
            enc.close()
            self._enc = None
            return False
        self._last_unsub = time.time()
        # The priming frame is already encoded; the _run loop must not
        # re-encode it on its first tick when the screen is static.
        self._last_encoded = rgb
        self._last_encode_t = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="h264-broadcast")
        self._thread.start()
        return True

    @property
    def running(self):
        return (self._thread is not None and self._thread.is_alive()
                and not self._stopped)

    def subscriber_count(self):
        with self._lock:
            return len(self._subs)

    def subscribe(self):
        """Attach a viewer; returns H264Subscriber, or None if dead.

        The subscriber's first read() returns the init segment, followed
        by the most recent IDR fragment when one has been encoded — so a
        late joiner gets instantly-decodable video even on a static screen
        (live fragments alone might be P-frames referencing a long-gone
        IDR). Live fragments follow as usual.
        """
        with self._lock:
            if not self.running or self._init is None:
                return None
            sub = H264Subscriber(self._init,
                                 max_queue_bytes=self._max_queue_bytes)
            self._subs.append(sub)
            if self._last_idr is not None:
                sub._push(self._last_idr)
            return sub

    def unsubscribe(self, sub):
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
            self._last_unsub = time.time()
        sub.close()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=5)
        self._cleanup()

    # -- internals ------------------------------------------------------
    def _cleanup(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            subs = self._subs
            self._subs = []
        for s in subs:
            s.close()
        enc, self._enc = self._enc, None
        if enc is not None:
            enc.close()

    def _rescale(self, rgb, w, h, tw, th):
        try:
            from PIL import Image
            import io as _io
            img = Image.frombytes("RGB", (w, h), rgb)
            img = img.resize((tw, th), Image.BILINEAR)
            return img.tobytes()
        except Exception:
            return None

    def _feed(self, data):
        """Parse encoder output into init + moof-aligned fragments."""
        if not data:
            return
        self._buf += data
        boxes, self._buf = split_mp4_boxes(self._buf)
        for btype, bdata in boxes:
            if btype == b"moof":
                if self._init is None:
                    if self._pre:
                        self._init = self._pre
                        self._pre = b""
                        self._cur = bdata
                    else:
                        # moof before any init boxes: hold it, keep waiting
                        # for a real init (start() will time out if the
                        # encoder never emits ftyp/moov).
                        self._pre += bdata
                else:
                    if self._cur:
                        if _fragment_has_idr(self._cur):
                            with self._lock:
                                self._last_idr = self._cur
                        self._fanout(self._cur)
                    self._cur = bdata
            elif self._init is None and not self._cur:
                self._pre += bdata
            else:
                self._cur += bdata

    def _fanout(self, fragment):
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s._push(fragment)

    def _run(self):
        interval = 1.0 / self.fps
        # Frameless guard: with a subscribed viewer but a dead frame source
        # this loop would spin at fps forever, hammering the source (for
        # remote instances: an HTTP fetch per tick) while the encoder idles.
        # Break instead — subscribers are closed, the dashboard falls back
        # to MJPEG, and a later viewer re-creates the broadcaster.
        last_frame = time.time()
        try:
            while not self._stop.is_set():
                t0 = time.time()
                try:
                    got = self._frame_source()
                except Exception:
                    got = None
                if got is not None and self._enc is not None:
                    try:
                        rgb, w, h = got
                    except (TypeError, ValueError):
                        got = None
                if got is not None and self._enc is not None:
                    last_frame = t0
                    if (w, h) != (self._enc_w, self._enc_h):
                        rgb = self._rescale(rgb, w, h,
                                            self._enc_w, self._enc_h)
                    if rgb is not None:
                        # Static-skip: a byte-identical frame adds nothing
                        # visually; skip the libx264 encode (the dominant
                        # per-tick cost). A heartbeat encode every
                        # static_heartbeat seconds keeps fragments flowing
                        # (dashboard stall badge stays honest, late joiners
                        # stay near a fresh fragment). The frameless-guard
                        # clock above still ticks on every fetched frame.
                        if (self._last_encoded is None
                                or rgb != self._last_encoded
                                or t0 - self._last_encode_t
                                >= self._static_heartbeat):
                            if not self._enc.write_frame(rgb):
                                break  # encoder died
                            self._last_encoded = rgb
                            self._last_encode_t = t0
                        elif not self._enc.alive():
                            break  # encoder died while idling on static
                elif time.time() - last_frame > self._frameless_timeout:
                    break  # frameless guard: no frames for frameless_timeout
                if self._enc is not None:
                    self._feed(self._enc.read_available(timeout=0))
                with self._lock:
                    idle = (not self._subs
                            and time.time() - self._last_unsub > self._idle_timeout)
                if idle:
                    break
                dt = time.time() - t0
                if dt < interval:
                    self._stop.wait(interval - dt)
        finally:
            self._cleanup()
