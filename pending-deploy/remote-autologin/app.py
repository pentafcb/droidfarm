#!/usr/bin/env python3
"""
Jerusalem 1.0 instance manager + dashboard backend.
Stdlib-only (http.server + manual WebSocket). Manages Waydroid-in-Docker
instances on the host via the docker CLI.

Env:
  JERUSALEM_PASSWORD  dashboard password (default: changeme - change it!)
  JERUSALEM_PORT      listen port (default 8080)
  JERUSALEM_DATA      data dir (default /root/jerusalem)
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

# WebRTC (aiortc) — real-time video streaming
try:
    import asyncio
    import numpy as np
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    from av import VideoFrame
    HAS_AIORTC = True
except ImportError:
    HAS_AIORTC = False

DATA_DIR = os.environ.get("JERUSALEM_DATA", "/root/jerusalem")
PASSWORD = os.environ.get("JERUSALEM_PASSWORD", "changeme")
# Docker named-volume root on the manager host. Monkeypatched by tests.
VOLUMES_BASE = "/var/lib/docker/volumes"

# --- HTTP server bounds: slowloris / thread-starvation hardening ---
# Stock ThreadingHTTPServer spawns one thread per connection with no cap and
# no per-connection timeout: a handful of half-open or dribble-feed clients
# (or a wedged proxy) pins handler threads forever and starves the whole
# manager — fast 200 TTFB then mid-body connection drops, exactly the wedge
# observed 2026-10-11. BoundedHTTPServer caps concurrent handler threads
# (excess connections fail fast with 503 instead of piling up threads) and
# drops connections idle longer than the socket timeout.
HTTP_MAX_THREADS = 64    # concurrent handler threads; legit peak is < 20
HTTP_CONN_TIMEOUT = 60   # seconds of socket inactivity before the drop
HTTP_LISTEN_BACKLOG = 128


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = HTTP_LISTEN_BACKLOG
    # Class attrs so tests can subclass with tighter bounds.
    max_threads = HTTP_MAX_THREADS
    conn_timeout = HTTP_CONN_TIMEOUT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(type(self).max_threads)

    def get_request(self):
        req, addr = super().get_request()
        req.settimeout(type(self).conn_timeout)
        return req, addr

    def process_request(self, request, client_address):
        # Fail fast when saturated: a 503 for the newcomer beats an
        # unbounded thread pile-up that would wedge every other client.
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Connection: close\r\n"
                                b"Content-Length: 0\r\n\r\n")
            except OSError:
                pass
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()
            return
        t = threading.Thread(target=self._process_request_guarded,
                             args=(request, client_address),
                             daemon=True, name="http-handler")
        t.start()

    def _process_request_guarded(self, request, client_address):
        # Same as ThreadingMixIn.process_request_thread, plus slot release.
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            try:
                self.shutdown_request(request)
            finally:
                self._slots.release()

def check_dashboard_password(pw):
    """Constant-time dashboard password check.

    Never raises: non-string input (JSON null/number/list) and non-ASCII
    input (which would make hmac.compare_digest on str raise TypeError and
    turn /api/login into a 500) both return False instead of crashing.
    """
    if not isinstance(pw, str):
        return False
    try:
        return hmac.compare_digest(pw.encode("utf-8"), PASSWORD.encode("utf-8"))
    except Exception:
        return False


def _prune_login_attempts(now):
    """Drop expired failure timestamps; then enforce the hard IP cap by
    evicting stalest-first. Best-effort, never raises. Eviction is fail-open
    for the evicted IP (its counter resets) — the cap only matters against a
    massively distributed spray, where per-IP limits can't help anyway."""
    try:
        for ip in list(LOGIN_ATTEMPTS.keys()):
            recent = [t for t in LOGIN_ATTEMPTS[ip] if now - t < LOGIN_RATE_WINDOW]
            if recent:
                LOGIN_ATTEMPTS[ip] = recent
            else:
                LOGIN_ATTEMPTS.pop(ip, None)
        if len(LOGIN_ATTEMPTS) > LOGIN_RATE_MAX_IPS:
            ordered = sorted(LOGIN_ATTEMPTS.items(),
                             key=lambda kv: kv[1][-1] if kv[1] else 0)
            for ip, _ in ordered[:len(ordered) - LOGIN_RATE_MAX_IPS]:
                LOGIN_ATTEMPTS.pop(ip, None)
    except Exception:
        pass


def login_allowed(ip):
    """Sliding-window rate limit on failed /api/login attempts, per IP.
    Never raises; unknown IPs are always allowed."""
    now = time.time()
    try:
        with _lock:
            atts = [t for t in LOGIN_ATTEMPTS.get(ip, [])
                    if now - t < LOGIN_RATE_WINDOW]
            if atts or ip in LOGIN_ATTEMPTS:
                LOGIN_ATTEMPTS[ip] = atts
            if len(LOGIN_ATTEMPTS) > LOGIN_RATE_MAX_IPS:
                _prune_login_attempts(now)
            return len(atts) < LOGIN_RATE_MAX
    except Exception:
        return True


def record_login_failure(ip):
    """Count one failed dashboard login for ip. Never raises."""
    try:
        with _lock:
            atts = LOGIN_ATTEMPTS.get(ip, [])
            atts.append(time.time())
            LOGIN_ATTEMPTS[ip] = atts
    except Exception:
        pass


def clear_login_failures(ip):
    """A successful login resets the IP's failure counter. Never raises."""
    try:
        with _lock:
            LOGIN_ATTEMPTS.pop(ip, None)
    except Exception:
        pass


def _client_ip_from_headers(headers, peer):
    """Client key for the public rate limiters, proxy-aware. Never raises.

    Cloudflare tunnel (cloudflared) terminates the public connection and
    opens a fresh local connection to the manager, so every tunneled
    dashboard user shares the same TCP peer (loopback). Keying the
    /api/login and /api/providers/register limiters on the peer alone
    would make one user's 10 mistyped passwords 429 everyone on the
    tunnel. When the peer is a loopback address, prefer the client IP
    the proxy reports: CF-Connecting-IP first, then the leftmost
    X-Forwarded-For entry (the original client). The candidate must
    parse as an IP literal or the peer is kept (fail closed to the old
    behavior). Headers are NEVER trusted for non-loopback peers — they
    are trivially spoofable on the open internet.
    """
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return peer
        get = getattr(headers, "get", None)
        if not callable(get):
            return peer
        for name in ("CF-Connecting-IP", "X-Forwarded-For"):
            raw = get(name)
            if not raw:
                continue
            cand = str(raw).split(",")[0].strip()
            try:
                return str(ipaddress.ip_address(cand))
            except ValueError:
                continue
        return peer
    except Exception:
        return peer


PORT = int(os.environ.get("JERUSALEM_PORT", "8080"))
INSTANCES_FILE = os.path.join(DATA_DIR, "instances.json")
TOKENS_FILE = os.path.join(DATA_DIR, "tokens.json")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "roblox_accounts.json")
LOGIN_RESULTS_FILE = os.path.join(DATA_DIR, "roblox_login_results.json")

NETWORK = "droid-net"
SUBNET_BASE = "10.20.0."
IP_START = 10
IMAGE = "waydroid-instance:latest"
# Display resolution inside the container (for input scaling)
DISP_W, DISP_H = 1024, 608
# Android-side scratch dir (bind-mounted to host path below)
DROID_SCRATCH = "/data/media/0/jerusalem"
HOST_SCRATCH = "/root/.local/share/waydroid/data/media/0/jerusalem"

os.makedirs(DATA_DIR, exist_ok=True)

# Try PIL for JPEG conversion (smaller frames); fall back to PNG.
try:
    from PIL import Image
    import io as _io
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# H.264-over-WebSocket (fMP4 + MSE) streaming — real video compression that
# works through the tunnel (no UDP/ICE needed like WebRTC).
try:
    from h264_stream import H264Encoder, H264Broadcaster, HAS_FFMPEG
    HAS_H264 = bool(HAS_FFMPEG and HAS_PIL)
except ImportError:
    HAS_H264 = False

_lock = threading.Lock()
_tokens = {}  # token -> expiry epoch

# ---------- multi-host provider system ----------
PROVIDERS_FILE = os.path.join(DATA_DIR, "providers.json")
JOIN_TOKENS = {}  # token -> {created, used}
REGISTER_ATTEMPTS = {}  # ip -> [timestamps] for rate limiting
LOGIN_ATTEMPTS = {}  # ip -> [timestamps of FAILED dashboard logins] for rate limiting
LOGIN_RATE_WINDOW = 60     # seconds: sliding window for failed logins
LOGIN_RATE_MAX = 10        # failed logins per window per IP before 429
LOGIN_RATE_MAX_IPS = 1024  # hard bound on tracked IPs (anti-memory-growth)
INSTALL_PROGRESS = {}  # install_id -> {hostname, os, stage, message, updated_at, ip}
WING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wing")
WING_IMAGE_TAR = os.path.join(DATA_DIR, "wing-image.tar")
STATIC_TOKEN_FILE = os.path.join(DATA_DIR, "static_join_token")


def get_static_token():
    try:
        with open(STATIC_TOKEN_FILE) as f:
            t = f.read().strip()
            if len(t) >= 32:
                return t
            had_invalid = True
    except FileNotFoundError:
        had_invalid = False
    except OSError:
        # Unreadable (permissions, is-a-directory): regenerate below, loudly.
        had_invalid = True
    if had_invalid:
        # A torn write (kill -9 / ENOSPC between open("w") truncate and
        # write) used to silently ROTATE the "static" join token here: the
        # short read failed the length check, a new token was generated with
        # no log line, and every saved node-enrollment one-liner started
        # 403ing with no visible cause. Say it loudly so a rotation is never
        # a mystery again.
        print("[join-token] WARNING: static token file exists but is "
              "invalid/unreadable - regenerating. Re-issue node enrollment "
              "one-liners from the dashboard.", flush=True)
    t = secrets.token_hex(32)
    # Atomic write (tmp+rename, same idiom as save_providers/save_instances):
    # a crash mid-write can no longer leave a truncated token behind.
    tmp = STATIC_TOKEN_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(t)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, STATIC_TOKEN_FILE)
    except OSError:
        # Persist failed (read-only dir, etc.): keep serving this process
        # from the in-memory token rather than crashing the manager. The
        # token rotates on restart - warned above.
        print("[join-token] WARNING: could not persist static token "
              "(%s) - using in-memory token for this process lifetime."
              % STATIC_TOKEN_FILE, flush=True)
    return t


def load_providers():
    try:
        with open(PROVIDERS_FILE) as f:
            d = json.load(f)
            # A non-dict file (hand edit, disk corruption — the atomic
            # saves make a truncated file near-impossible, but a valid
            # JSON list/string is not) used to flow verbatim into every
            # caller, where .items()/.get() raised AttributeError and
            # killed the handler thread with no response. Same shape
            # rule as load_login_results: non-dict means "no providers",
            # exactly like a missing file.
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        # OSError covers FileNotFoundError (missing) plus PermissionError,
        # IsADirectoryError and I/O errors; ValueError covers
        # JSONDecodeError (malformed) plus UnicodeDecodeError (bad bytes).
        # An unreadable state file must degrade to "no state" exactly like
        # a missing one — the old narrow tuple let both classes escape and
        # kill the handler thread with no response on every caller.
        return {}


def provider_last_seen(p):
    """Numeric last_seen epoch from a provider record, else 0.

    t152/t153 fixed the providers FILE shape, but a record inside it can
    still be the wrong shape (a hand edit, a partial write): a non-dict
    record, or a string last_seen, raised AttributeError/TypeError in
    the create/resize/pick/providers-list paths and killed the handler
    thread with no response. 0 means "recency unknown" — every caller
    treats the node as offline, never as online.
    """
    if not isinstance(p, dict):
        return 0
    ls = p.get("last_seen", 0)
    if isinstance(ls, bool) or not isinstance(ls, (int, float)):
        return 0
    return ls


def provider_stats(p):
    """Stats dict from a provider record, else {} (never raises)."""
    if not isinstance(p, dict):
        return {}
    st = p.get("stats")
    return st if isinstance(st, dict) else {}


def instance_provider_of(record):
    """Provider id for an instance record, or None when it is unusable.

    The instance-side twin of the t154 provider-record guard: a RECORD
    inside a valid instances.json dict can still be the wrong shape (a
    hand edit, a partial write), and every caller that did
    ``instances.get(name, {}).get("provider", "local")`` then crashed
    with AttributeError on a string record — killing the handler thread
    with no response, or (in the /api/instances loop) the whole fleet
    poll. A missing record means "local" (the historic default, and
    callers with their own not-found check still decide); a missing
    provider field in a dict record also means "local". A present but
    non-dict record, or a present but non-string/empty provider, is
    invalid -> None, and every caller treats None as "instance not
    found" (never as local, never as a crash). Never raises.
    """
    if record is None:
        return "local"
    if not isinstance(record, dict):
        return None
    if "provider" not in record:
        return "local"
    prov = record.get("provider")
    return prov if isinstance(prov, str) and prov else None


def account_of(record):
    """A usable Roblox account record, or {} when it is unusable.

    The account-side twin of instance_provider_of (t155): a RECORD
    inside a valid roblox_accounts.json dict can still be the wrong
    shape (a hand edit, a partial write), and every caller that did
    ``accounts.get(name, {}).get(\"username\")`` or
    ``acct[\"username\"] = ...`` then crashed with AttributeError /
    TypeError on a string record — killing the handler thread with no
    response, or (in roblox_account_summary) the whole fleet coverage
    poll. A non-dict record degrades to {} (no username, no password):
    read paths report "no account", write paths replace the corrupt
    record with the new fields (it held no credentials to preserve).
    Never raises.
    """
    return record if isinstance(record, dict) else {}


def save_providers(data):
    tmp = PROVIDERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    # 600 BEFORE the replace (providers-600): providers.json holds every
    # node's agent_key — the credential the manager presents to wing
    # agents — but this save never chmodded anything, so the live file
    # (and any crashed tmp beside it) sat world-readable under umask 022.
    # The /api/providers response already strips agent_key, so the disk
    # file was the only leak path. os.replace preserves the tmp's mode,
    # so chmod-first is what makes the live file 600 from birth; the
    # next heartbeat save after deploy fixes already-deployed files.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, PROVIDERS_FILE)


def gen_secret(n=32):
    return secrets.token_urlsafe(n)


def wing_request(provider, method, path, data=None, timeout=8, attempts=3):
    """HTTP request to a wing agent. Returns (status_code, parsed_json_or_bytes).

    attempts: how many times to retry a failed connection (default 3, the
    historical behavior). High-frequency hot paths (dashboard polls,
    per-frame stream fetches) pass attempts=1 to fail fast: retrying
    there only adds latency and pins handler threads, because the caller
    already retries at the loop level (poll cadence, reconnect, resubscribe).
    User-initiated one-shots (input, launch, create, delete, login) keep
    the default so a single transient reset doesn't fail the action.
    """
    import urllib.request as _ureq
    import urllib.error as _uerr
    import http.client as _httpc
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        attempts = 3
    if attempts < 1:
        attempts = 1
    # A provider record missing its url/agent_key (hand edit, partial
    # write) used to raise KeyError here and kill the caller's handler
    # thread with no response. Fail closed as "node unreachable" — every
    # caller already handles 502 — instead of crashing on the record.
    if not isinstance(provider, dict):
        return 502, {"error": "node unreachable: bad provider record"}
    url_raw = provider.get("url")
    key_raw = provider.get("agent_key")
    if (not isinstance(url_raw, str) or not url_raw
            or not isinstance(key_raw, str) or not key_raw):
        return 502, {"error": "node unreachable: bad provider record"}
    url = url_raw.rstrip("/") + path
    body = json.dumps(data).encode() if data is not None else None
    last_err = None
    for attempt in range(attempts):
        req = _ureq.Request(url, data=body, method=method,
                            headers={"X-Agent-Key": key_raw,
                                     "Content-Type": "application/json",
                                     "Connection": "close"})
        try:
            with _ureq.urlopen(req, timeout=timeout) as r:
                ct = r.headers.get("Content-Type", "")
                raw = r.read(2097152 if "json" in ct else 20971520)
                if "json" in ct:
                    return r.status, json.loads(raw.decode()) if raw else {}
                return r.status, raw
        except _uerr.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {"error": "http error"}
        except (_httpc.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(1)
                continue
            return 502, {"error": f"node unreachable: {e}"}
        except Exception as e:
            return 502, {"error": f"node unreachable: {e}"}
    return 502, {"error": f"node unreachable: {last_err}"}


# ---- remote instance status cache -------------------------------------------
# The dashboard polls GET /api/instances every 10s. A wedged-but-heartbeating
# wing node (last_seen < 120s) would stall every poll for the full retry
# budget (up to ~17s at timeout=5/attempts=3), serially, per provider — and
# each stalled poll holds a BoundedHTTPServer handler thread. The poll path
# therefore fetches with attempts=1 (fail fast) and, on failure, reuses the
# last-known-good snapshot for _REMOTE_STATUS_TTL seconds instead of
# flickering every remote card to "stopped". Best-effort, never raises.
_REMOTE_STATUS_TTL = 30
_remote_status_cache = {}  # pid -> (monotonic_ts, {(pid, name): status_dict})
# Consecutive failed /api/agent/instances fetches per provider. A
# heartbeating node whose status endpoint keeps failing reads "stopped"
# once the 30s cache above expires — node_offline() does not cover it
# (heartbeats still flow), so this count drives the card badge below.
# Best-effort, never raises; pruned to known provider ids each call.
REMOTE_FETCH_DEGRADED_AFTER = 6
_remote_fetch_failures = {}  # pid -> consecutive failures


def fetch_remote_statuses(providers):
    """Fetch /api/agent/instances from every recently-seen wing node.

    Returns {(provider_id, name): status_dict}. On a fetch failure the
    last-known snapshot is reused while it is younger than
    _REMOTE_STATUS_TTL; older than that (or never seen) the node simply
    contributes nothing — remote cards then read "stopped", honestly.
    Never raises.
    """
    out = {}
    now = time.monotonic()
    try:
        items = list((providers or {}).items())
    except Exception:
        return out
    if items:
        # drop counters for providers that no longer exist; a live
        # provider's counter is never dropped (it is in items)
        known = {pid for pid, _p in items}
        for k in list(_remote_fetch_failures):
            if k not in known:
                del _remote_fetch_failures[k]
    for pid, p in items:
        try:
            if not isinstance(p, dict):
                continue
            # provider must have heartbeated recently
            if time.time() - p.get("last_seen", 0) > 120:
                continue
            code, resp = wing_request(p, "GET", "/api/agent/instances",
                                     timeout=5, attempts=1)
            if code == 200 and isinstance(resp, dict):
                per = {}
                insts = resp.get("instances", [])
                if isinstance(insts, (list, tuple)):
                    for i in insts:
                        if isinstance(i, dict) and isinstance(i.get("name"), str):
                            per[(pid, i["name"])] = i
                _remote_status_cache[pid] = (now, per)
                _remote_fetch_failures[pid] = 0
                out.update(per)
            else:
                _remote_fetch_failures[pid] = \
                    _remote_fetch_failures.get(pid, 0) + 1
                ts, per = _remote_status_cache.get(pid, (0, {}))
                if isinstance(per, dict) and now - ts <= _REMOTE_STATUS_TTL:
                    out.update(per)
        except Exception:
            # wing_request raised (timeout, connection error, ...): the
            # endpoint did not answer, so it counts like a failed fetch —
            # but the last-known-good cache is still served, exactly like
            # the non-200 branch above. Before this fix the raise path
            # skipped the cache entirely, so a single transient timeout
            # (the most common failure mode) flipped every card on that
            # node to "stopped" at once, while the node-fetch badge needs
            # 6 consecutive failures before it says anything.
            _remote_fetch_failures[pid] = _remote_fetch_failures.get(pid, 0) + 1
            ts, per = _remote_status_cache.get(pid, (0, {}))
            if isinstance(per, dict) and now - ts <= _REMOTE_STATUS_TTL:
                out.update(per)
            continue
    return out


def patch_remote_status(pid, name, running):
    """Optimistically patch the last-known-good remote status after a
    manager-initiated remote mutation (start/stop/restart/create).

    fetch_remote_statuses() reuses the cached snapshot when the live fetch
    fails, so without this a flaky poll right after e.g. a Stop would show
    the pre-action state ("running") for up to _REMOTE_STATUS_TTL seconds.
    Only the instance's `running` flag is touched; every other field stays
    as last fetched, and a missing entry gets a minimal {"name", "running"}
    dict. The next successful fetch replaces the snapshot wholesale, so the
    patch can never stick. Never raises."""
    try:
        spid, sname = str(pid), str(name)
        now = time.monotonic()
        cur = _remote_status_cache.get(spid)
        per = dict(cur[1]) if cur and isinstance(cur[1], dict) else {}
        old = per.get((spid, sname))
        new = dict(old) if isinstance(old, dict) else {"name": sname}
        new["running"] = bool(running)
        per[(spid, sname)] = new
        _remote_status_cache[spid] = (now, per)
    except Exception:
        pass


def drop_remote_status(pid, name):
    """Remove one instance from the last-known-good remote cache after a
    successful remote delete, so a stale entry can never render a card for
    an instance the manager no longer tracks. Never raises."""
    try:
        spid, sname = str(pid), str(name)
        cur = _remote_status_cache.get(spid)
        if cur and isinstance(cur[1], dict):
            per = dict(cur[1])
            per.pop((spid, sname), None)
            _remote_status_cache[spid] = (cur[0], per)
    except Exception:
        pass


def pick_provider(providers, need_ram_mb=4096, need_cpus=2):
    """Auto-pick the least-loaded provider with enough free capacity.
    'local' is always a candidate."""
    cands = []
    # local capacity
    ls = get_host_stats()
    local_used_ram = sum(v.get("memory_mb", 0) for v in load_instances().values()
                         if isinstance(v, dict)
                         and v.get("provider", "local") == "local")
    if ls["ram_total_mb"] - local_used_ram >= need_ram_mb:
        cands.append(("local", ls["ram_percent"]))
    for pid, p in providers.items():
        # A non-dict record, or string stats/last_seen fields inside one,
        # used to raise AttributeError/TypeError here and kill the
        # create handler (provider="auto") with no response. A record
        # that cannot prove its capacity/recency is simply not a
        # candidate — never a crash.
        if not isinstance(p, dict):
            continue
        st = provider_stats(p)
        total = st.get("ram_total_mb", 0)
        if (isinstance(total, bool) or not isinstance(total, (int, float))
                or not total):
            continue
        pct = st.get("ram_percent", 0)
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        # rough used = reported percent; instances on that provider tracked by main
        used_by_us = sum(v.get("memory_mb", 0) for v in load_instances().values()
                         if isinstance(v, dict) and v.get("provider") == pid)
        free_mb = total * (1 - pct / 100) - used_by_us
        # provider must have heartbeated recently
        if time.time() - provider_last_seen(p) > 120:
            continue
        if free_mb >= need_ram_mb:
            cands.append((pid, pct))
    if not cands:
        return None
    cands.sort(key=lambda x: x[1])
    return cands[0][0]


def load_instances():
    try:
        with open(INSTANCES_FILE) as f:
            d = json.load(f)
            # Non-dict state file: same guard as load_providers — a JSON
            # list/string here used to crash every caller that did
            # .items()/.get() (including the dashboard's /api/instances
            # poll), dropping the request with no response.
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        # Same unreadable-means-no-state rule as load_providers.
        return {}


def save_instances(data):
    tmp = INSTANCES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    # 600 BEFORE the replace (state-600-complete): instances.json was
    # the last manager state save with no chmod at all — under umask 022
    # it sat world-readable, exposing the fleet's instance names and
    # internal IPs. os.replace preserves the tmp's mode, so chmod-first
    # is what makes the live file 600 from birth; the next save after
    # deploy repairs already-deployed files automatically.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, INSTANCES_FILE)


def load_accounts():
    """Load Roblox account credentials per instance. Format: {name: {username, password, auto_login}}"""
    try:
        with open(ACCOUNTS_FILE) as f:
            d = json.load(f)
            # Non-dict state file: same guard as load_providers — a JSON
            # list/string here used to crash every caller that did
            # .items()/.get() on the accounts mapping.
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        # Same unreadable-means-no-state rule as load_providers.
        return {}


def save_accounts(data):
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    # 600 BEFORE the replace (state-tmp-sweep): the old code chmodded
    # after the replace, so the live file sat world-readable between the
    # two calls — and a crash in between left a world-readable tmp
    # holding every fleet credential behind. os.replace preserves the
    # tmp's mode, so chmod-first is what makes the live file 600.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ACCOUNTS_FILE)


def load_login_results():
    """Last recorded Roblox login attempt per instance.

    Format: {name: {ok, ts, reason, by, attempts}}. Outcome only —
    never stores credentials.
    """
    try:
        with open(LOGIN_RESULTS_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        # Same unreadable-means-no-state rule as load_providers.
        return {}


def save_login_results(data):
    tmp = LOGIN_RESULTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    # 600 BEFORE the replace (state-600-complete): the old code chmodded
    # the live file AFTER the replace, so a crash in between left a
    # world-readable tmp behind and the live file was briefly exposed
    # on every save. Outcome-only file, but the chmod-after pattern was
    # the last instance of it in the manager; chmod-first closes it.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, LOGIN_RESULTS_FILE)


def record_login_result(name, ok, reason, by="manual"):
    """Record the outcome of one Roblox login attempt. Best-effort: never
    raises. One entry per instance (last result only, bounded); attempts
    counted. Records no credentials — reasons are server-generated messages."""
    try:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(name))[:64]
        if not safe:
            return
        by = by if by in ("auto", "manual", "batch") else "manual"
        results = load_login_results()
        prev = results.get(safe) if isinstance(results.get(safe), dict) else {}
        results[safe] = {
            "ok": bool(ok),
            "ts": int(time.time()),
            "reason": str(reason or "")[:160],
            "by": by,
            "attempts": int(prev.get("attempts") or 0) + 1,
        }
        save_login_results(results)
    except Exception:
        pass


def drop_login_result(name):
    """Drop one instance's last login outcome (delete-state-sweep).

    Delete removes the container and volumes but the outcome record in
    roblox_login_results.json survived — a recreated instance with the
    same name inherited the dead instance's login badge ("login OK"
    from a login that never happened on the new instance) until a new
    attempt overwrote it. Returns True when an entry was removed.
    Never raises. Saved accounts (roblox_accounts.json) are NOT touched
    here: credentials are never deleted by the delete path."""
    try:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(name))[:64]
        if not safe:
            return False
        results = load_login_results()
        if safe not in results:
            return False
        del results[safe]
        save_login_results(results)
        return True
    except Exception:
        return False


# ---- bulk Roblox account import -------------------------------------------
# Text format, one instance per line:
#   name,username,password[,auto]
# Lines starting with # and blank lines are skipped. The optional 4th field
# enables auto_login (accepts 1/true/yes/auto/y, case-insensitive).

_AUTO_TRUE = {"1", "true", "yes", "auto", "y"}


def parse_bulk_accounts(text):
    """Parse bulk import text. Returns (entries, line_errors).

    entries: [{name, username, password, auto_login}]
    line_errors: [{line, error}] for lines that could not be parsed.

    Passwords may contain commas: the name is the text before the first
    comma, the username the text between the first and second comma, and
    the password is everything after the second comma. A trailing
    comma-separated segment that is exactly an auto literal (1/true/yes/
    auto/y, case-insensitive) is the auto-login flag and is stripped from
    the password. Ambiguity note: a password that literally ends in
    ",true" (etc.) cannot be imported this way — set it in the
    per-instance Account panel instead."""
    entries, line_errors = [], []
    for lineno, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            line_errors.append({"line": lineno,
                                "error": "need name,username,password"})
            continue
        name, username = parts[0].strip(), parts[1].strip()
        tail = parts[2:]
        auto = False
        if len(parts) > 3 and parts[-1].strip().lower() in _AUTO_TRUE:
            auto = True
            tail = tail[:-1]
        password = ",".join(tail).strip()
        if not name:
            line_errors.append({"line": lineno,
                                "error": "missing instance name"})
            continue
        if not username:
            line_errors.append({"line": lineno,
                                "error": "missing username"})
            continue
        if not password:
            line_errors.append({"line": lineno,
                                "error": "missing password"})
            continue
        entries.append({"name": name, "username": username[:100],
                        "password": password[:200], "auto_login": auto})
    return entries, line_errors


def bulk_save_accounts(entries, instances, force_auto=False):
    """Save validated bulk entries. Returns (saved, failed).

    instances: {name: ...} from load_instances() — names not in it are
    refused. force_auto overrides every entry's auto_login to True."""
    saved, failed = [], []
    accounts = load_accounts()
    for e in entries:
        if e["name"] not in instances:
            failed.append({"name": e["name"], "error": "unknown instance"})
            continue
        # account_of: a corrupt (non-dict) record is replaced by the new
        # entry instead of crashing on item assignment (it held no
        # credentials to preserve).
        acct = account_of(accounts.get(e["name"]))
        acct["username"] = e["username"]
        acct["password"] = e["password"]
        acct["auto_login"] = True if force_auto else bool(e["auto_login"])
        accounts[e["name"]] = acct
        saved.append(e["name"])
    if saved:
        save_accounts(accounts)
    return saved, failed


def roblox_account_summary():
    """Fleet-wide account coverage + last login outcomes.
    Never includes passwords."""
    instances = load_instances()
    accounts = load_accounts()
    results = load_login_results()
    out = {}
    for name in instances:
        # account_of: one corrupt record must not kill the whole fleet
        # coverage poll (a string record crashed .get here before).
        acct = account_of(accounts.get(name))
        lr = results.get(name) if isinstance(results.get(name), dict) else None
        out[name] = {
            "username": acct.get("username", ""),
            "has_password": bool(acct.get("password")),
            "auto_login": bool(acct.get("auto_login", False)),
            "login_ok": lr.get("ok") if lr else None,
            "login_ts": lr.get("ts") if lr else None,
            "login_by": lr.get("by") if lr else None,
            "login_reason": lr.get("reason") if lr else None,
            "login_attempts": lr.get("attempts") if lr else 0,
        }
    covered = sum(1 for a in out.values()
                  if a["username"] and a["has_password"])
    logged_in = sum(1 for a in out.values() if a["login_ok"] is True)
    return {"accounts": out, "covered": covered, "logged_in": logged_in,
            "total": len(instances)}


def load_tokens():
    global _tokens
    try:
        with open(TOKENS_FILE) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            # A non-dict tokens file used to raise AttributeError on
            # .items() — and main() calls load_tokens() unguarded, so
            # the manager died at startup into a systemd restart loop
            # (total dashboard outage) instead of serving with no
            # sessions, which is what a missing file already means.
            _tokens = {}
        else:
            # Drop malformed entries individually: a single non-numeric
            # expiry used to raise TypeError on `e > time.time()` and
            # take down the same startup path; valid sessions survive.
            _tokens = {t: e for t, e in raw.items()
                       if isinstance(e, (int, float)) and e > time.time()}
    except (OSError, ValueError):
        # Same unreadable-means-no-state rule as load_providers: a bad-bytes
        # or directory tokens file used to raise out of main()'s unguarded
        # load_tokens() call and kill the manager at startup.
        _tokens = {}


def save_tokens():
    # Atomic write (tmp+rename): a crash between open("w") truncate and
    # write used to leave a truncated tokens.json behind; the next boot's
    # load_tokens() then hit JSONDecodeError and logged EVERY dashboard
    # session out. Best-effort, never raises.
    try:
        tmp = TOKENS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_tokens, f)
        # 600 before the replace (state-tmp-sweep): the old code never
        # chmodded at all, so tokens.json sat world-readable permanently
        # (and every crashed tmp beside it). Same idiom as the static
        # token write and the batch journal.
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, TOKENS_FILE)
    except OSError:
        pass


def run(cmd, timeout=30):
    """Run a shell command, return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def run_bytes(cmd, timeout=30):
    """Run a shell command, return (rc, stdout_bytes)."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        return 124, b""


# ---- instance-name gate (shell-injection guard) ----
# Every instance id the manager ever creates matches ^[A-Za-z0-9_-]{1,32}$
# (single-create strips anything else, fleet-create generates prefix+N, the
# agent's create fullmatch-rejects). The ([^/]+) route regexes do NOT
# constrain the name, so a raw path segment must never reach a shell=True
# command: every route that captures an instance name gates it here, and the
# two shell sinks below (docker_inspect, waydroid_exec) gate as defense in
# depth so a hand-edited instances.json can never inject either. Invalid ->
# 404 "instance not found", indistinguishable from an unknown instance.
_INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def valid_instance_name(name):
    """True when name is a create-time-valid instance id. Never raises."""
    try:
        return bool(_INSTANCE_NAME_RE.fullmatch(name or ""))
    except Exception:
        return False


def valid_ipv4(s):
    """True when s is a strict dotted-quad IPv4 literal. Never raises.

    The instance IP is interpolated into the `docker run` command line
    (shell) in create_local_instance, so only a plain IPv4 literal is
    acceptable — hostnames, IPv6, CIDR, and anything carrying shell
    metacharacters are rejected. (create-ip-gate: an authenticated
    dashboard user could otherwise pass {"ip": "10.20.0.5;touch${IFS}/x"}
    and get root shell execution on the manager host.)
    """
    try:
        t = str(s or "").strip()
        if not t or any(c.isspace() for c in t):
            return False
        ipaddress.IPv4Address(t)
        return True
    except Exception:
        return False


def docker_inspect(name):
    # Sink gate (defense in depth): every legitimate caller passes
    # "wd-<valid instance name>"; anything else fails closed.
    if not re.fullmatch(r"wd-[A-Za-z0-9_-]{1,32}", name or ""):
        return None
    rc, out, _ = run(f"docker inspect {name} 2>/dev/null", timeout=10)
    if rc != 0:
        return None
    try:
        return json.loads(out)[0]
    except (json.JSONDecodeError, IndexError):
        return None


def container_status(name):
    info = docker_inspect(name)
    if not info:
        return "missing"
    return "running" if info.get("State", {}).get("Running") else "stopped"


def container_ip(name):
    info = docker_inspect(name)
    if not info:
        return None
    nets = info.get("NetworkSettings", {}).get("Networks", {})
    for net in nets.values():
        if net.get("IPAddress"):
            return net["IPAddress"]
    return None


def instance_health(name):
    """Watchdog state for a local instance.

    df-watchdog.sh (minute cron) writes <name>.degraded into DF_ST when an
    instance keeps failing and auto-recovery pauses, and <name>.roblox_drift
    when the installed Roblox client has drifted off the pinned build. Each
    marker's first line is the human-readable reason (v4.1+); older/empty
    markers get a generic message. Surfacing this lets the dashboard show
    "needs attention" / "Roblox actualizado" instead of a misleading green
    "ready" card. Best-effort: never raises.
    """
    try:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(name))
        st = os.environ.get("DF_ST", "/tmp/df-watchdog")

        def _marker(suffix, generic):
            p = os.path.join(st, safe + suffix)
            if not os.path.isfile(p):
                return False, None
            reason = None
            try:
                with open(p, "r") as f:
                    line = f.readline().strip()
                    if line:
                        reason = line[:200]
            except OSError:
                pass
            return True, reason or generic

        degraded, dreason = _marker(".degraded",
                                    "auto-recovery paused by watchdog")
        drift, drift_reason = _marker(".roblox_drift",
                                      "Roblox client drifted off pinned build")
        return {"degraded": degraded, "reason": dreason,
                "drift": drift, "drift_reason": drift_reason}
    except Exception:
        return {"degraded": False, "reason": None,
                "drift": False, "drift_reason": None}


# Filenames in the watchdog state dir that belong to the farm, not to any
# instance — a same-named instance must never sweep them (an instance
# literally called "docker" shares its .degraded filename with the global
# docker-daemon marker, and the watchdog itself cannot tell them apart
# either, so the sweep leaves globals alone rather than guessing).
_WATCHDOG_GLOBAL_FILES = {
    "manager.degraded", "docker.degraded", "disk.degraded", "mem.degraded",
    "boot.degraded",
    "disk.prune.ev", "mem.low.ev", "mem.oom.ev", "log.trim.ev",
    "docker.restart.ev", "mgr.restart.ev",
}


def drop_watchdog_state(name):
    """Remove per-instance watchdog state files for a deleted instance
    (delete-state-sweep). df-watchdog.sh discovers instances from
    instances.json, so a deleted instance's markers were never cleaned:
    <name>.degraded / .roblox_drift / .robloxver / .sessionstart.ts,
    the .ev circuit-breaker windows, .helper-missing-<script>.<date>
    files, and .dw-timeout.<date> probe markers. A recreated instance
    with the same name inherited the dead instance's "needs attention"
    badge (degraded has priority over the setup badge) and its
    version-drift badge through the whole boot window, because the
    watchdog only clears markers once ALL checks go green. Returns the
    number of files removed. Best-effort: never raises."""
    try:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(name))
        if not safe:
            return 0
        st = os.environ.get("DF_ST", "/tmp/df-watchdog")
        removed = 0

        def _drop(fn):
            nonlocal removed
            if fn in _WATCHDOG_GLOBAL_FILES:
                return
            p = os.path.join(st, fn)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed += 1
            except OSError:
                pass

        for suffix in (".degraded", ".roblox_drift", ".robloxver",
                       ".sessionstart.ts"):
            _drop(safe + suffix)
        try:
            names = os.listdir(st)
        except OSError:
            return removed
        for fn in names:
            # Exact-name prefix: "rbx1." never matches "rbx10.degraded".
            if not fn.startswith(safe + "."):
                continue
            if (fn.endswith(".ev") or ".helper-missing-" in fn
                    or ".dw-timeout." in fn):
                _drop(fn)
        return removed
    except Exception:
        return 0


# ---- Global watchdog health (dashboard banner) ----
# df-watchdog.sh writes GLOBAL markers into DF_ST when a farm-wide
# auto-recovery budget trips: manager.degraded (manager-service restarts
# over budget), docker.degraded (dockerd restarts over budget),
# disk.degraded (disk auto-prune over budget), mem.degraded (host memory
# pressure, 3 consecutive low checks), boot.degraded (host-boot fleet
# recovery paused: the binder pool is blocked or incomplete, so no
# instance starts until a human clears it or the pool recovers).
# Unlike per-instance <name>.degraded
# markers (surfaced by instance_health() as card badges), these were only
# visible as transient lines in the Eventos log — once the log scrolled
# past, the dashboard kept showing green cards while farm-wide
# auto-recovery was paused. global_watchdog_health() surfaces them so
# GET /api/instances can carry them to a dashboard banner. Same
# conventions as instance_health(): first line is the reason, truncated
# to 200 chars, generic fallback for empty markers. Best-effort: never
# raises; returns [] (all green) on any failure.
#
# PLUS the watchdog's own liveness (watchdog-heartbeat-banner): the
# minute-cron touches $ST/heartbeat on every executed run (after the
# lock is held — a run blocked by another run's lock never touches it,
# so a wedged lock-holder also ages the heartbeat). A dead cron means
# NO auto-recovery at all, yet the dashboard showed green: markers only
# report trips, never the absence of the watcher. So a missing heartbeat
# (state dir exists, watchdog never completed a run) or one older than
# WATCHDOG_HEARTBEAT_MAX_AGE appends a "watchdog" entry AFTER the marker
# entries — trips first, the meta-state last, so the marker ordering
# contract is unchanged. 600s (10 missed minute-runs) is deliberately
# looser than verify-live.sh's 300s WARN: a persistent banner must not
# flash on one slow run, it must catch a dead cron. A missing state dir
# stays [] (never fired yet is indistinguishable from a fresh install
# there, and the old "missing dir -> []" contract holds). Wing nodes
# need no equivalent: a dead agent stops heartbeating and the
# node-offline badge already covers it.
WATCHDOG_HEARTBEAT_MAX_AGE = 600
GLOBAL_WATCHDOG_MARKERS = (
    ("manager", "manager.degraded", "manager auto-recovery paused"),
    ("docker", "docker.degraded", "docker auto-recovery paused"),
    ("disk", "disk.degraded", "disk auto-prune paused"),
    ("memory", "mem.degraded", "host memory pressure"),
    ("boot", "boot.degraded", "boot recovery paused"),
)


def global_watchdog_health(st_dir=None):
    """Farm-wide watchdog markers currently tripped.

    Returns a list of {"key", "reason"} for each global marker file
    present in the watchdog state dir, in GLOBAL_WATCHDOG_MARKERS order,
    plus a trailing {"key": "watchdog"} entry when the watchdog's own
    liveness heartbeat is missing or stale (see above); [] when all
    green. Only the known global marker filenames are ever read (no
    instance names involved, so no sanitizing needed).
    """
    try:
        st = st_dir if st_dir is not None else os.environ.get(
            "DF_ST", "/tmp/df-watchdog")
        out = []
        for key, fname, generic in GLOBAL_WATCHDOG_MARKERS:
            p = os.path.join(st, fname)
            if not os.path.isfile(p):
                continue
            reason = None
            try:
                with open(p, "r") as f:
                    line = f.readline().strip()
                    if line:
                        reason = line[:200]
            except OSError:
                pass
            out.append({"key": key, "reason": reason or generic})
        # Watchdog liveness: only when the state dir itself exists.
        try:
            if os.path.isdir(st):
                hb = os.path.join(st, "heartbeat")
                if not os.path.isfile(hb):
                    out.append({"key": "watchdog",
                                "reason": "watchdog minute-cron never "
                                          "fired (no heartbeat yet)"})
                else:
                    try:
                        age = int(time.time() - os.path.getmtime(hb))
                    except OSError:
                        age = None
                    if age is None:
                        out.append({"key": "watchdog",
                                    "reason": "watchdog heartbeat "
                                              "unreadable"})
                    elif age > WATCHDOG_HEARTBEAT_MAX_AGE:
                        out.append({"key": "watchdog",
                                    "reason": "watchdog minute-cron not "
                                              "firing (heartbeat %ds ago)"
                                              % age})
        except Exception:
            pass
        return out
    except Exception:
        return []


# ---- Nodes attention summary (dashboard Nodes-button badge) ----
# Remote-node selfwatch DEGRADED maps already surface inside the Nodes
# modal (per-node "needs attention" badge), but the topbar Nodos button
# itself gave no signal — a degraded wing node was invisible until Penta
# opened the modal. GET /api/instances now carries nodes_attention so
# the button badges on every grid refresh. Remote-only by construction:
# the providers dict holds wing nodes (the local node is a pseudo-provider
# added only to the /api/providers response, never here). Staleness rule
# matches the Eventos merge and the /api/providers "online" flag: a node
# not seen in 120s contributes nothing, so a dead node's last-known
# degraded map can't pin the badge forever. Best-effort, never raises.
NODES_ATTENTION_STALE_S = 120


def nodes_attention_summary(providers=None):
    """Wing nodes whose selfwatch reports a non-empty degraded map.

    Returns a list of {"node", "markers", "reason"} — node is the display
    name (or provider id), markers the degraded marker keys, reason the
    first marker's reason. [] when no live node needs attention.
    """
    try:
        if providers is None:
            providers = load_providers()
        if not isinstance(providers, dict):
            return []
        now = time.time()
        out = []
        for pid, p in providers.items():
            try:
                if not isinstance(p, dict):
                    continue
                try:
                    last_seen = float(p.get("last_seen", 0))
                except (TypeError, ValueError):
                    continue
                if now - last_seen > NODES_ATTENTION_STALE_S:
                    continue
                sw = p.get("selfwatch")
                if not isinstance(sw, dict):
                    continue
                deg = sw.get("degraded")
                if not isinstance(deg, dict) or not deg:
                    continue
                markers = []
                reason = ""
                for k, v in deg.items():
                    mk = str(k)[:32]
                    if not mk:
                        continue
                    markers.append(mk)
                    if not reason and isinstance(v, str) and v:
                        reason = v[:160]
                if not markers:
                    continue
                node = str(p.get("name") or pid)[:40]
                out.append({"node": node, "markers": markers,
                            "reason": reason})
            except Exception:
                continue
        return out
    except Exception:
        return []


# ---- Node-offline card badge -----------------------------------------------
# A remote instance on a wing node that stopped heartbeating contributes
# nothing to fetch_remote_statuses(), so its card reads "stopped" — exactly
# like an instance the user deliberately stopped. The card therefore
# carries node_offline so the dashboard can say which one it is. Same 120s
# staleness rule as nodes_attention_summary() and the /api/providers
# "online" flag; a missing provider record (node deleted, instances
# lingering) counts as offline. Best-effort, never raises. This is about
# heartbeat staleness only: a heartbeating node whose /api/agent/instances
# fetch fails still serves its 30s last-known-good cache, then reads
# "stopped" without the badge — that case is transient by construction.
NODE_OFFLINE_STALE_S = 120


def node_offline(providers, prov):
    """True when a wing node's heartbeat is stale or its record is missing.

    Local instances never call this (the manager answering the poll IS the
    local node); the handler emits False for them directly.
    """
    try:
        if not isinstance(providers, dict):
            return True
        p = providers.get(prov)
        if not isinstance(p, dict):
            return True
        try:
            last_seen = float(p.get("last_seen", 0))
        except (TypeError, ValueError):
            return True
        return time.time() - last_seen > NODE_OFFLINE_STALE_S
    except Exception:
        return True


# ---- Node-fetch-failure card badge -----------------------------------------
# The mirror case node_offline() deliberately excludes: a wing node that
# KEEPS heartbeating (so it is not "offline") but whose /api/agent/instances
# endpoint keeps failing. fetch_remote_statuses() serves the 30s
# last-known-good cache, then the node's cards read "stopped" — exactly
# like instances the user deliberately stopped — with no badge at all.
# That case is only "transient by construction" while failures are
# transient; once REMOTE_FETCH_DEGRADED_AFTER consecutive polls fail
# (~60s at the dashboard's 10s cadence) it is persistent enough to say
# so. The badge ranks below node_offline (a dead node outranks a failing
# endpoint) and above degraded/drift (both come from the last-known-good
# cache and are stale once fetches fail). Best-effort, never raises.
def remote_fetch_degraded(providers, prov):
    """True when a heartbeating wing node's status endpoint has failed
    REMOTE_FETCH_DEGRADED_AFTER consecutive polls.

    A missing provider record or a stale heartbeat returns False: those
    are node_offline()'s territory, and the two badges never fire together.
    """
    try:
        if not isinstance(providers, dict):
            return False
        p = providers.get(prov)
        if not isinstance(p, dict):
            return False
        try:
            last_seen = float(p.get("last_seen", 0))
        except (TypeError, ValueError):
            return False
        if time.time() - last_seen > NODE_OFFLINE_STALE_S:
            return False
        return _remote_fetch_failures.get(prov, 0) >= REMOTE_FETCH_DEGRADED_AFTER
    except Exception:
        return False


def remote_fetch_failures(prov):
    """Consecutive failed status fetches for one provider (tooltip count).

    Never raises; 0 when unknown."""
    try:
        return int(_remote_fetch_failures.get(str(prov), 0))
    except Exception:
        return 0


# ---- Watchdog event feed (dashboard "Eventos" view) ----
# df-watchdog.sh appends one line per recovery/action to /var/log/df-watchdog.log
# (the log() helper trims it to its last 300 lines at 2MB). Penta watches the
# dashboard, not SSH, so this feeds an events modal: the last N lines,
# severity-tagged. The log path is FIXED (never from user input); the line
# count is clamped to [1, 200]. Best-effort: never raises — worst case the
# dashboard shows "sin datos" instead of a broken modal.
WATCHDOG_LOG = os.environ.get("DF_WATCHDOG_LOG", "/var/log/df-watchdog.log")
WATCHDOG_EVENTS_MAX = 200


def watchdog_event_severity(line):
    """Tag one log line for the events view. Never raises."""
    try:
        # recovery patterns first: "recovered from DEGRADED" is good news,
        # not an alert.
        if "recovered" in line or "back on pinned" in line or \
                "started ok" in line or "restarted ok" in line:
            return "ok"
        if "ALERT" in line or "DEGRADED" in line:
            return "alert"
        if "WARN" in line:
            return "warn"
        return "info"
    except Exception:
        return "info"


def read_watchdog_events(lines=100):
    """Tail the watchdog log.

    Returns {"ok", "path", "events": [{"line", "sev"}]}. Never raises; when
    the log is missing/unreadable, ok=False and events=[] so the dashboard
    can say "sin datos" honestly instead of showing an empty list.
    """
    try:
        n = max(1, min(int(lines), WATCHDOG_EVENTS_MAX))
    except (TypeError, ValueError):
        n = 100
    try:
        with open(WATCHDOG_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read().decode("utf-8", errors="replace")
        raw = chunk.splitlines()
        if size > 65536 and raw:
            raw = raw[1:]  # first line may be a partial read
        tail = raw[-n:]
        events = [{"line": ln[:500], "sev": watchdog_event_severity(ln)}
                  for ln in tail]
        return {"ok": True, "path": WATCHDOG_LOG, "events": events}
    except Exception:
        return {"ok": False, "path": WATCHDOG_LOG, "events": []}


BACKUP_STATUS_FILE = os.path.join(DATA_DIR, "last-backup.json")


def read_backup_status():
    """Read the last-backup.json marker written by backup-state.sh.

    Returns {"known", "ok", "ts", "age_h", "archive", "bytes", "error"}.
    The script writes the marker on EVERY run (success and failure), so a
    missing/unreadable/corrupt marker returns known=False — the dashboard
    says "sin datos" honestly instead of guessing. Never raises; field
    types are strictly validated so a hand-edited marker can't inject
    garbage into the dashboard.
    """
    try:
        with open(BACKUP_STATUS_FILE, "rb") as f:
            d = json.loads(f.read().decode("utf-8", errors="replace"))
        if not isinstance(d, dict):
            raise ValueError("marker is not a dict")
        age = None
        try:
            epoch = int(d.get("ts_epoch"))
            age = max(0.0, (time.time() - epoch) / 3600.0)
        except (TypeError, ValueError):
            pass
        ts = d.get("ts")
        ts = ts if isinstance(ts, str) else None
        arch = d.get("archive")
        arch = arch if isinstance(arch, str) else None
        nbytes = d.get("bytes")
        nbytes = nbytes if isinstance(nbytes, int) and nbytes >= 0 else None
        err = d.get("error")
        err = err if isinstance(err, str) else ""
        return {"known": True, "ok": d.get("ok") is True, "ts": ts,
                "age_h": age, "archive": arch, "bytes": nbytes,
                "error": err[:160]}
    except Exception:
        return {"known": False, "ok": False, "ts": None, "age_h": None,
                "archive": None, "bytes": None, "error": ""}


def _event_ts(line):
    """Parse the [YYYY-MM-DD HH:MM:SS] prefix of a watchdog log line.

    The manager and the wing agent both write this prefix (time.strftime
    "%F %T"), so events from every node carry comparable timestamps for
    the merged Eventos view. Returns epoch seconds or None when the
    prefix is missing/malformed. Never raises.
    """
    try:
        if not isinstance(line, str) or len(line) < 22:
            return None
        if not line.startswith("[") or line[20] != "]":
            return None
        return time.mktime(time.strptime(line[1:20], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def fetch_node_watchdog_events(providers, lines=50):
    """Fetch selfwatch events from wing nodes for the Eventos modal.

    Returns a list of {"line", "sev", "node", "ts"}. Best-effort, never
    raises: offline or stale-heartbeated (>120s) nodes are skipped, old
    agents (404 on the new route) contribute nothing, and failures are
    silent — a dead node simply shows no events. Fetches run
    attempts=1 (fail fast); the modal re-polls every 15s anyway, and the
    15s frameless/hot-path reasoning applies: no one-shot here needs a
    retry budget.
    """
    events = []
    try:
        items = list((providers or {}).items())
    except Exception:
        return events
    try:
        n = max(1, min(int(lines), WATCHDOG_EVENTS_MAX))
    except (TypeError, ValueError):
        n = 50
    for pid, p in items:
        try:
            if not isinstance(p, dict):
                continue
            if time.time() - p.get("last_seen", 0) > 120:
                continue
            node = str(p.get("name") or pid)[:40]
            code, resp = wing_request(p, "GET",
                                     "/api/agent/selfwatch-events?lines=%d" % n,
                                     timeout=5, attempts=1)
            if code != 200 or not isinstance(resp, dict) or not resp.get("ok"):
                continue
            raw = resp.get("events", [])
            if not isinstance(raw, (list, tuple)):
                continue
            for e in raw:
                if not isinstance(e, dict):
                    continue
                line = e.get("line")
                if not isinstance(line, str):
                    continue
                line = line[:500]
                events.append({"line": line,
                               "sev": watchdog_event_severity(line),
                               "node": node, "ts": _event_ts(line)})
        except Exception:
            continue
    return events


def merge_watchdog_events(local_events, node_events):
    """Merge main-node and wing-node events chronologically (oldest first).

    All events carry "ts" (epoch or None) and "node" (None = main node).
    Stable sort: equal/absent timestamps keep their input order (locals
    first, then nodes in fetch order), unparseable lines trail. The "ts"
    key is stripped afterwards — the dashboard only sees line/sev/node.
    Never raises.
    """
    try:
        combined = list(local_events or []) + list(node_events or [])

        def _key(e):
            ts = e.get("ts") if isinstance(e, dict) else None
            return (ts is None, ts if isinstance(ts, (int, float)) else 0)

        combined.sort(key=_key)
        for e in combined:
            if isinstance(e, dict):
                e.pop("ts", None)
        return combined
    except Exception:
        return list(local_events or [])


# ---- Per-instance docker snapshot cache (10s TTL) ----
# GET /api/instances feeds every dashboard poll (every 10s per client). Each
# poll ran `docker inspect` once per local instance — 20 serialized
# subprocess spawns on a full fleet, adding seconds of latency to every
# refresh. The cache holds one snapshot per instance (docker status + IP +
# marker files + health) for 10s, matching the dashboard cadence, so a poll
# costs one inspect per instance per 10s window at most. Mutation endpoints
# (start/stop/restart/delete) invalidate the entry, so the next poll shows
# fresh state. Best-effort throughout: cache bugs must never break the
# response, so every helper never raises.
_snapshot_cache = {}  # safe_name -> {"ts": float, "data": {...}}
_snapshot_lock = threading.Lock()
_SNAPSHOT_TTL = 10


def _instance_snapshot_fresh(safe, inst):
    """Build one snapshot from live sources. Never raises."""
    try:
        cname = "wd-" + safe
        info = docker_inspect(cname)
        if not info:
            status, ip = "missing", None
        else:
            status = "running" if info.get("State", {}).get("Running") else "stopped"
            ip = (inst or {}).get("ip")
            if not ip:
                nets = info.get("NetworkSettings", {}).get("Networks", {})
                for net in nets.values():
                    if isinstance(net, dict) and net.get("IPAddress"):
                        ip = net["IPAddress"]
                        break
        vol_base_new = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/jerusalem"
        vol_base_old = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/droidfarm"
        booted = (os.path.exists(vol_base_new + "/booted") or
                  os.path.exists(vol_base_old + "/booted"))
        roblox_done = (os.path.exists(vol_base_new + "/roblox_done") or
                       os.path.exists(vol_base_old + "/roblox_done"))
        if status == "running" and booted and roblox_done:
            setup = "ready"
        elif status == "running" and booted:
            setup = "installing_roblox"
        elif status == "running":
            setup = "booting"
        else:
            setup = status  # stopped / missing
        health = instance_health(safe)
        return {"status": status, "ip": ip, "setup": setup,
                "degraded": health["degraded"],
                "degraded_reason": health["reason"],
                "roblox_drift": health["drift"],
                "roblox_drift_reason": health["drift_reason"]}
    except Exception:
        return {"status": "missing", "ip": None, "setup": "missing",
                "degraded": False, "degraded_reason": None,
                "roblox_drift": False, "roblox_drift_reason": None}


def instance_snapshot(name, inst):
    """Cached per-instance snapshot for /api/instances. Never raises.

    Returns a fresh dict each call (callers can mutate it freely)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(name))[:64]
    now = time.time()
    try:
        with _snapshot_lock:
            v = _snapshot_cache.get(safe)
            if v and now - v["ts"] <= _SNAPSHOT_TTL:
                return dict(v["data"])
    except Exception:
        pass
    data = _instance_snapshot_fresh(safe, inst if isinstance(inst, dict) else {})
    try:
        with _snapshot_lock:
            _snapshot_cache[safe] = {"ts": now, "data": data}
    except Exception:
        pass
    return dict(data)


def invalidate_instance_snapshot(name):
    """Drop one cached snapshot (after start/stop/restart/delete). Never raises."""
    try:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(name))[:64]
        with _snapshot_lock:
            _snapshot_cache.pop(safe, None)
    except Exception:
        pass


# ---- Live per-instance resource usage (docker stats cache) ----
# The 31GB host carries 20x 2GB instance limits (40GB of limits): limits are
# not usage, so per-instance live RAM/CPU is what actually guards headroom.
# One background thread polls `docker stats --no-stream` every 15s (a single
# subprocess for ALL containers, not one per instance) and caches results in
# memory. All readers are best-effort and never raise; entries go stale after
# 60s so a wedged poller degrades to "no data" instead of lying.
_resource_cache = {}  # name -> {"mem_used_mb": float, "cpu_pct": float, "ts": float}
_resource_lock = threading.Lock()
_RESOURCE_TTL = 60
_RESOURCE_INTERVAL = 15


def drop_resource_cache(name):
    """Drop one instance's cached docker-stats entry (delete-state-sweep).

    The poller prunes stale entries on its next pass anyway, but an
    explicit drop means a recreated instance never briefly renders the
    dead instance's last usage. Best-effort: never raises."""
    try:
        with _resource_lock:
            _resource_cache.pop(name, None)
    except Exception:
        pass

_SIZE_MULT = {"B": 1 / 1048576, "KB": 1 / 1024, "MB": 1, "GB": 1024, "TB": 1048576,
              "KIB": 1 / 1024, "MIB": 1, "GIB": 1024, "TIB": 1048576}


def parse_size_mb(s):
    """'412.5MiB'/'1.2GiB'/'512KiB'/'100B' -> float MB, None on garbage."""
    try:
        m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", str(s))
        if not m:
            return None
        mult = _SIZE_MULT.get(m.group(2).upper())
        if mult is None:
            return None
        return float(m.group(1)) * mult
    except (ValueError, TypeError, AttributeError):
        return None


def parse_docker_stats(output):
    """Parse `docker stats --no-stream --format '{{.Name}}\\t{{.MemUsage}}\\t{{.CPUPerc}}'`.

    Returns {instance_name: {"mem_used_mb": f, "cpu_pct": f}} for wd-* containers.
    Skips malformed lines, non-wd names, and unsafe names (same sanitization
    as the rest of the manager).
    """
    out = {}
    for line in (output or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        cname, memusage, cpuperc = parts
        if not cname.startswith("wd-"):
            continue
        name = cname[3:]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            continue
        memparts = memusage.split("/")
        if len(memparts) != 2:
            continue
        used = parse_size_mb(memparts[0])
        if used is None:
            continue
        try:
            cpu = float(cpuperc.strip().rstrip("%"))
        except (ValueError, TypeError, AttributeError):
            continue
        if cpu != cpu or cpu == float("inf") or cpu == float("-inf"):
            continue  # NaN/inf would render as garbage on the dashboard
        out[name] = {"mem_used_mb": round(used, 1), "cpu_pct": round(cpu, 1)}
    return out


def _resource_poll_once(docker_stats_fn=None):
    """One cache refresh. docker_stats_fn (test seam) returns raw stats text."""
    try:
        if docker_stats_fn is None:
            p = subprocess.run(
                ["docker", "stats", "--no-stream", "--format",
                 "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"],
                capture_output=True, text=True, timeout=30)
            raw = p.stdout if p.returncode == 0 else ""
        else:
            raw = docker_stats_fn()
        parsed = parse_docker_stats(raw)
        now = time.time()
        with _resource_lock:
            for name, v in parsed.items():
                _resource_cache[name] = {"mem_used_mb": v["mem_used_mb"],
                                         "cpu_pct": v["cpu_pct"], "ts": now}
            # prune stale so missing/deleted containers stop reporting
            stale = [k for k, v in _resource_cache.items()
                     if now - v["ts"] > _RESOURCE_TTL]
            for k in stale:
                del _resource_cache[k]
    except Exception:
        pass  # never raise; keep last good data


# ---- adaptive screencap -----------------------------------------------------
# The in-container screencap loop (waydroid-image/screencap-loop.sh) captures
# fast (0.12s) while a viewer is attached and idles at 2s otherwise — the old
# 8fps-always loop burned host CPU on all 20 instances 24/7 for nobody (the
# dashboard cards show no thumbnails; the only live.png consumers are active
# stream viewers). The loop decides from the mtime of the stream_wanted
# marker in the instance's jerusalem dir: fresh (< _STREAM_WANT_STALE) means
# a viewer is attached. A stale marker (manager restart left it behind)
# degrades to slow mode on its own.
_STREAM_WANT_STALE = 30
_STREAM_WANT_BASE = "/var/lib/docker/volumes"

_stream_viewers = {}  # (prov, name) -> active ws viewer count
_stream_viewers_lock = threading.Lock()


def _stream_want_path(name, base=None):
    """Host path of the stream_wanted marker for a local instance, or None."""
    if base is None:
        base = _STREAM_WANT_BASE
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
            return None
        return ("%s/wd-%s-data/_data/data/media/0/jerusalem/stream_wanted"
                % (base, name))
    except Exception:
        return None


def _touch_stream_want(name, base=None):
    """Create/re-touch the marker. Best-effort, never raises."""
    if base is None:
        base = _STREAM_WANT_BASE
    try:
        p = _stream_want_path(name, base)
        if not p:
            return
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
        except Exception:
            pass
        with open(p, "a"):
            os.utime(p, None)
    except Exception:
        pass


def _clear_stream_want(name, base=None):
    """Remove the marker. Best-effort, never raises."""
    if base is None:
        base = _STREAM_WANT_BASE
    try:
        p = _stream_want_path(name, base)
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def stream_viewer_open(prov, name):
    """Record one attached ws viewer; local instances get a fresh marker.

    Remote instances are a no-op here — the wing agent tracks its own
    screenshot-fetch traffic and maintains its marker autonomously.
    Best-effort, never raises.
    """
    try:
        if prov != "local":
            return
        with _stream_viewers_lock:
            key = (prov, name)
            _stream_viewers[key] = _stream_viewers.get(key, 0) + 1
        _touch_stream_want(name)
    except Exception:
        pass


def stream_viewer_close(prov, name):
    """Drop one ws viewer; the last one out clears the marker."""
    try:
        if prov != "local":
            return
        with _stream_viewers_lock:
            key = (prov, name)
            n = _stream_viewers.get(key, 0) - 1
            if n <= 0:
                _stream_viewers.pop(key, None)
                n = 0
            else:
                _stream_viewers[key] = n
        if n == 0:
            _clear_stream_want(name)
    except Exception:
        pass


def stream_want_retouch():
    """Re-touch markers for instances with active viewers.

    Called every 15s from resource_poller so long-lived viewers keep the
    in-container loop fast (15s < _STREAM_WANT_STALE). Best-effort.
    """
    try:
        with _stream_viewers_lock:
            actives = [name for (prov, name), n in _stream_viewers.items()
                       if prov == "local" and n > 0]
        for name in actives:
            _touch_stream_want(name)
    except Exception:
        pass


def resource_poller():
    """Background loop: never dies, never raises."""
    while True:
        try:
            _resource_poll_once()
            stream_want_retouch()  # adaptive-screencap: keep viewer markers fresh
        except Exception:
            pass
        time.sleep(_RESOURCE_INTERVAL)


def instance_resources(name):
    """Live {mem_used_mb, cpu_pct} for a local instance, or None (best-effort)."""
    try:
        with _resource_lock:
            v = _resource_cache.get(name)
        if not v or time.time() - v["ts"] > _RESOURCE_TTL:
            return None
        return {"mem_used_mb": v["mem_used_mb"], "cpu_pct": v["cpu_pct"]}
    except Exception:
        return None


def clean_agent_resource(v):
    """Sanitize one usage number reported by a node agent.

    float in [0, 1e6], rounded to 1 decimal; anything else -> None.
    Best-effort, never raises — a bad agent payload must not break /api/instances.
    """
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: float() of a huge int (json 10**400) raises, and
        # json Infinity reaches float() fine but is range-rejected below.
        return None
    if not 0 <= f <= 1e6:
        return None
    return round(f, 1)


def clean_agent_drift(v):
    """Sanitize the roblox_drift flag reported by a node agent (>=1.0.5).

    Strict True only — older agents omit the field -> False (same as "no data").
    Best-effort, never raises.
    """
    return v is True


def clean_agent_drift_reason(v):
    """Sanitize the drift reason string from a node agent (>=1.0.5).

    Non-string/empty -> None; truncated to 160 like the agent's degraded-reason
    convention. Best-effort, never raises.
    """
    try:
        if not isinstance(v, str):
            return None
        s = v.strip()
        return s[:160] if s else None
    except Exception:
        return None


def clean_agent_degraded(v):
    """Sanitize the degraded flag reported by a node agent (>=1.0.22).

    Strict True only — older agents omit the field -> False (same as "no
    data"). Best-effort, never raises.
    """
    return v is True


def clean_agent_degraded_reason(v):
    """Sanitize the degraded reason string from a node agent (>=1.0.22).

    Non-string/empty -> None; truncated to 160 like the agent's marker
    convention. Best-effort, never raises.
    """
    try:
        if not isinstance(v, str):
            return None
        s = v.strip()
        return s[:160] if s else None
    except Exception:
        return None


def clean_agent_host_caps(raw):
    """Sanitize the host_caps dict reported by a node agent (>=1.0.27).

    Returns {"loop_devices": int|None, "image_present": bool|None} —
    None per field means "no data" (older agent, Windows node, or a probe
    that could not determine the fact). Best-effort, never raises.
    """
    try:
        if not isinstance(raw, dict):
            return {"loop_devices": None, "image_present": None}
        try:
            loop = int(raw.get("loop_devices"))
            if not 0 <= loop <= 4096:
                loop = None
        except (TypeError, ValueError):
            loop = None
        img = raw.get("image_present")
        return {"loop_devices": loop,
                "image_present": img if isinstance(img, bool) else None}
    except Exception:
        return {"loop_devices": None, "image_present": None}


def host_mem_total_mb():
    """Host RAM in MB from /proc/meminfo, None if unreadable."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


# Wing-node staleness for fleet totals: same 120s rule as the /api/providers
# online flag and nodes_attention_summary, so a dead node's last-known
# usage/RAM can't pin the fleet bar forever.
REMOTE_FLEET_STALE_S = 120


def _fleet_number(v):
    """One usage number for the fleet totals: finite float in [0, 1e6].

    Same validation as clean_agent_resource (defined below; resolved at call
    time): a bad agent payload must never corrupt the fleet bar. Anything
    else -> None. Best-effort, never raises."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: float() of a huge int (json 10**400) raises.
        return None
    if f != f or f in (float("inf"), float("-inf")) or not 0 <= f <= 1e6:
        return None
    return round(f, 1)


def remote_host_ram_mb(providers):
    """Total RAM (MB) of wing nodes that heartbeated within the staleness window.

    Reads each provider record's heartbeat `stats.ram_total_mb` — the same
    field the fleet-create headroom check uses. A node with no stats, a
    stale/missing last_seen, or a garbage RAM value contributes 0, so a dead
    node never inflates the fleet bar's denominator. Best-effort, never
    raises — this feeds every /api/instances response."""
    total = 0
    try:
        items = list((providers or {}).items())
    except Exception:
        return 0
    now = time.time()
    for _pid, p in items:
        try:
            if not isinstance(p, dict):
                continue
            last_seen = p.get("last_seen", 0)
            if not isinstance(last_seen, (int, float)) or isinstance(last_seen, bool):
                continue
            if now - last_seen > REMOTE_FLEET_STALE_S:
                continue
            stats = p.get("stats")
            if not isinstance(stats, dict):
                continue
            ram = stats.get("ram_total_mb")
            if not isinstance(ram, (int, float)) or isinstance(ram, bool):
                continue
            if ram > 0:
                total += int(ram)
        except Exception:
            continue
    return total


def fleet_resource_summary(remote_instances=None, remote_host_ram_mb=0):
    """Actual fleet-wide usage from the cache (not limits): for the dashboard.

    Without arguments this is exactly the old local-only summary. With the
    /api/instances handler's remote entries, the totals become truly
    fleet-wide: remote instances' agent-reported mem/cpu (already cleaned
    per-card, re-validated here) are summed in, `instances_seen` counts
    local + remote reporting instances, and `host_mem_mb` is main-node RAM
    plus fresh wing-node RAM. cpu_pct stays a fleet-wide SUM of per-container
    CPU% (docker %CPU can exceed 100 on a multi-core host, so the sum was
    never a single-host percentage even when local-only). Shape is unchanged
    (same 4 keys) so existing dashboard/contract tests keep passing.
    Best-effort, never raises."""
    used = 0.0
    cpu = 0.0
    n = 0
    try:
        now = time.time()
        with _resource_lock:
            vals = [v for v in _resource_cache.values()
                    if now - v["ts"] <= _RESOURCE_TTL]
        for v in vals:
            used += v["mem_used_mb"]
            cpu += v["cpu_pct"]
            n += 1
    except Exception:
        pass
    try:
        for e in (remote_instances or []):
            if not isinstance(e, dict):
                continue
            mem = _fleet_number(e.get("mem_used_mb"))
            if mem is None:
                continue  # not seen in stats -> not counted, like local
            used += mem
            n += 1
            cpu_v = _fleet_number(e.get("cpu_pct"))
            if cpu_v is not None:
                cpu += cpu_v
    except Exception:
        pass
    host_mb = None
    try:
        host_mb = host_mem_total_mb()
    except Exception:
        pass  # this feeds every /api/instances response; it must never raise
    try:
        remote_ram = int(remote_host_ram_mb or 0)
    except Exception:
        remote_ram = 0
    if remote_ram > 0:
        host_mb = (host_mb or 0) + remote_ram
    return {"mem_used_mb": round(used, 1), "cpu_pct": round(cpu, 1),
            "instances_seen": n, "host_mem_mb": host_mb}


def next_instance_id(instances):
    """Allocate the next free INSTANCE_ID (unique int per instance)."""
    used = {inst.get("instance_id") for inst in instances.values()
            if isinstance(inst, dict)
            and isinstance(inst.get("instance_id"), int)}
    n = 0
    while n in used:
        n += 1
    return n


def ensure_binder_devices(count=64):
    """Create binderfs device nodes (binder, binder1..binderN) on demand.

    The manager only picks triplets from whatever exists in /dev/binderfs/
    (free_binder_triplet); a fresh host boot has the stock set, which caps
    the farm. Creating devices through binder-control makes the instance
    cap follow configuration instead of host boot state.

    On a brand-new host the binder_linux kernel module may never have been
    loaded at all (no binder devices exist) — then the binderfs mount below
    fails and every instance create errors with "no free binder devices".
    A best-effort `modprobe binder_linux` is attempted when binder-control
    is missing: a no-op when the module is already loaded, and a failure
    just falls through to the mount attempt with the same outcome as before
    (no regression). No user input is interpolated.
    Returns True if at least one device was created.
    """
    # Make sure binderfs is mounted.
    rc, _, _ = run("test -e /dev/binderfs/binder-control", timeout=10)
    if rc != 0:
        # Fresh host: load the binder kernel module first, or the mount
        # below cannot succeed. Best-effort, never raises.
        # NOTE: do NOT pass devices=<count> here. The binder 'devices'
        # module parameter is a comma-separated NAME list (kernel
        # CONFIG_ANDROID_BINDER_DEVICES semantics), not a count — a numeric
        # value creates a single device literally named "64" and zero
        # /dev/binder* nodes (this exact mistake shipped once and produced
        # no binder devices on a fresh host). The 64-device pool is built
        # by the BINDER_CTL_ADD loop below; persistent naming for fresh
        # installs lives in /etc/modprobe.d/binder.conf with the name list
        # (see install.sh).
        run("modprobe binder_linux 2>/dev/null", timeout=15)
        rc, _, _ = run("test -e /dev/binderfs/binder-control", timeout=10)
    if rc != 0:
        run("mkdir -p /dev/binderfs && mount -t binder binder /dev/binderfs 2>/dev/null", timeout=15)
        rc, _, _ = run("test -e /dev/binderfs/binder-control", timeout=10)
        if rc != 0:
            return False
    created = False
    for i in range(count):
        # Names are generated internally (no user input), safe to interpolate.
        name = "binder" if i == 0 else "binder%d" % i
        rc, _, _ = run("test -e /dev/binderfs/%s" % name, timeout=10)
        if rc != 0:
            rc2, _, _ = run("echo %s > /dev/binderfs/binder-control 2>/dev/null" % name, timeout=10)
            if rc2 == 0:
                created = True
    return created


def _prepare_pools_for_start():
    """Restore the binderfs + loop device pools before `docker start`.

    A host reboot drops both pools back to their stock sets, while the
    containers still bind-mount the specific /dev/binderfs/binderN devices
    they were created with. A raw `docker start` in that state makes docker
    auto-create EMPTY DIRECTORIES at the vanished bind sources -> dead
    binder mounts -> a stuck-boot wedge that then burns the watchdog's
    stuck-boot circuit budget. The boot-recovery automation (df-watchdog
    v14 section 0) repairs this in its own minute-cron path, but the
    dashboard's Start/Restart buttons and the batch "Start all"/"Restart
    all" call `docker start` directly — a start issued before or during
    boot recovery (or on a box whose watchdog hasn't run yet) would wedge
    the instance.

    Fast path: a single listdir; a complete pool costs zero subprocesses.
    Otherwise: rmdir empty docker bind-stub dirs (rmdir removes ONLY empty
    dirs, so it can never delete data; a non-empty stub is left for human
    cleanup) and restore the pool via ensure_binder_devices(); top up loop
    devices via ensure_loop_devices() when below the floor. Best-effort,
    never raises — on any failure the caller proceeds with the old behavior
    and the watchdog owns the wedge. Returns True when both phases ran
    without an unexpected exception.
    """
    try:
        try:
            try:
                names = set(os.listdir("/dev/binderfs"))
            except OSError:
                names = None
            if names is None:
                # Cannot even list the dir (missing/unmounted): let the full
                # ensure path (modprobe + mount + create) try its best.
                ensure_binder_devices()
            else:
                expected = {"binder"} | {"binder%d" % i
                                         for i in range(1, FLEET_BINDER_DEVICE_CAP)}
                missing = [n for n in expected if n not in names]
                stubs = [n for n in expected
                         if n in names and os.path.isdir("/dev/binderfs/" + n)]
                if missing or stubs:
                    for n in stubs:
                        # rmdir removes empty dirs only; a non-empty stub is
                        # left for human cleanup.
                        try:
                            os.rmdir("/dev/binderfs/" + n)
                        except OSError:
                            pass
                    ensure_binder_devices()
        except Exception:
            pass
        try:
            if count_loop_devices() < FLEET_LOOP_DEVICE_FLOOR:
                ensure_loop_devices()
        except Exception:
            pass
        return True
    except Exception:
        return False


FLEET_LOOP_DEVICE_COUNT = 64  # ensure_loop_devices() provisioning target
FLEET_LOOP_DEVICE_FLOOR = 60  # below this the loop pool is considered degraded


def count_loop_devices():
    """Number of /dev/loopN block nodes present. Best-effort, never raises."""
    try:
        rc, out, _ = run("ls -d /dev/loop[0-9]* 2>/dev/null", timeout=10)
        if rc != 0:
            return 0
        n = 0
        for tok in out.split():
            if re.fullmatch(r"/dev/loop\d+", tok):
                n += 1
        return n
    except Exception:
        return 0


def ensure_loop_devices(count=FLEET_LOOP_DEVICE_COUNT, floor=FLEET_LOOP_DEVICE_FLOOR):
    """Best-effort loop device provisioning for the Waydroid fleet.

    Each booted Waydroid instance consumes ~2 loop devices (image mounts);
    the 2026-10-08 fleet recovery needed ~40 for 20 instances. A fresh boot
    exposes only the stock set (8) unless the host was configured
    persistently (install.sh: `options loop max_loop=64` in
    /etc/modprobe.d/loop.conf + `loop` in /etc/modules) — the one-shot mknod
    at install time does NOT survive a reboot on devtmpfs.

    When fewer than `floor` nodes exist: load the loop module with
    max_loop=<count> if it isn't loaded yet (modprobe on an already-loaded
    module is a harmless no-op — max_loop cannot change on a live module),
    then mknod any missing nodes (covers devtmpfs quirks where the driver
    has capacity but the nodes are missing). A live module whose max_loop
    parameter reads below `floor` makes extra nodes useless (mknod cannot
    grow the driver's table), so that reports False honestly.

    Returns True when the pool is at/above `floor` afterwards.
    Best-effort, never raises; no user input interpolated (ints only).
    """
    try:
        count = int(count)
        floor = int(floor)
        if count_loop_devices() >= floor:
            return True
        rc, _, _ = run("test -d /sys/module/loop", timeout=10)
        if rc != 0:
            # Module not loaded at all: load it with the fleet-sized pool.
            run("modprobe loop max_loop=%d 2>/dev/null" % count, timeout=15)
        rc, out, _ = run("ls -d /dev/loop[0-9]* 2>/dev/null", timeout=10)
        present = set()
        if rc == 0:
            for tok in out.split():
                m = re.fullmatch(r"/dev/loop(\d+)", tok)
                if m:
                    present.add(int(m.group(1)))
        for i in range(count):
            if i not in present:
                run("mknod /dev/loop%d b 7 %d 2>/dev/null" % (i, i), timeout=10)
        if count_loop_devices() < floor:
            return False
        # Honesty check: nodes beyond the driver's max_loop are unusable.
        rc, out, _ = run("cat /sys/module/loop/parameters/max_loop 2>/dev/null",
                         timeout=10)
        if rc == 0:
            try:
                if int(out.strip()) < floor:
                    return False
            except ValueError:
                pass
        return True
    except Exception:
        return False


def free_binder_triplet(used):
    """Find 3 free binderfs devices. Returns [devname, devname, devname] or None."""
    rc, out, _ = run("ls /dev/binderfs/ 2>/dev/null", timeout=10)
    if rc != 0:
        return None
    devs = []
    for d in out.split():
        if d in ("binder",) or re.fullmatch(r"binder\d+", d):
            if d not in used:
                devs.append(d)
    # Prefer plain "binder" + numbered ones in order
    def sortkey(d):
        return (0, 0) if d == "binder" else (1, int(d[6:]))
    devs.sort(key=sortkey)
    return devs[:3] if len(devs) >= 3 else None


def used_binders(instances):
    used = set()
    for inst in instances.values():
        if not isinstance(inst, dict):
            continue
        binders = inst.get("binders", [])
        if isinstance(binders, list):
            for b in binders:
                used.add(b)
    # Also scan Docker for binder devices used by any container (including
    # unmanaged ones like wd-proto) to avoid conflicts. Devices may appear
    # in HostConfig.Devices (--device) or Mounts (bind mounts).
    rc, out, _ = run("docker ps -aq 2>/dev/null", timeout=15)
    if rc == 0 and out.strip():
        cids = out.split()
        rc2, out2, _ = run(f"docker inspect {' '.join(cids)} 2>/dev/null", timeout=20)
        if rc2 == 0:
            try:
                for info in json.loads(out2):
                    for d in info.get("HostConfig", {}).get("Devices", []):
                        m = re.fullmatch(r"/dev/binderfs/(binder\d*)", d.get("PathOnHost", ""))
                        if m:
                            used.add(m.group(1))
                    for mnt in info.get("Mounts", []):
                        m = re.fullmatch(r"/dev/binderfs/(binder\d*)", mnt.get("Source", ""))
                        if m:
                            used.add(m.group(1))
            except json.JSONDecodeError:
                pass
    return used


def next_free_ip(instances):
    used = {inst.get("ip") for inst in instances.values()
            if isinstance(inst, dict) and inst.get("ip")}
    # Also check docker network for IPs actually taken
    rc, out, _ = run(
        f"docker network inspect {NETWORK} --format '{{{{json .Containers}}}}' 2>/dev/null",
        timeout=10)
    if rc == 0:
        try:
            for c in json.loads(out).values():
                ip = (c.get("IPv4Address") or "").split("/")[0]
                if ip:
                    used.add(ip)
        except json.JSONDecodeError:
            pass
    for i in range(IP_START, 255):
        ip = f"{SUBNET_BASE}{i}"
        if ip not in used:
            return ip
    return None


def waydroid_exec(name, android_cmd, timeout=30):
    """
    Run a command inside Android via waydroid shell.
    android_cmd must not rely on dash-args reaching `waydroid` itself;
    we wrap it in a script file to be safe.
    Returns (rc, stdout).
    """
    # Sink gate (defense in depth): the container name is interpolated
    # into shell=True commands below; fail closed on anything the
    # create-time contract would never produce.
    if not valid_instance_name(name):
        return 1, ""
    cname = f"wd-{name}"
    # Per-call script name (waydroid-exec-cleanup): the old shared cmd.sh
    # was overwritten by every call and NEVER removed — it sat in the
    # instance volume forever holding the most recent command (viewer-typed
    # text via the do_instance_input fallback, the login script invocation),
    # readable from inside Android and from the host volume path. A unique
    # name also fixes the latent race where two concurrent waydroid_exec
    # calls on one instance clobbered each other's script. The file is
    # removed after the run on every path below (setup failure, exec
    # failure, timeout); a manager crash mid-call is the one path the
    # post-run rm cannot cover — sweep_stale_pwd_files() removes stale
    # cmd.sh / cmd-*.sh files at startup for exactly that crash.
    fname = "cmd-%s.sh" % secrets.token_hex(8)
    # Write script via a python one-liner to avoid quoting hell
    import shlex
    payload = base64.b64encode(android_cmd.encode()).decode()
    setup = (
        f"docker exec {cname} mkdir -p {HOST_SCRATCH} && "
        f"docker exec {cname} python3 -c "
        f"\"import base64; open('{HOST_SCRATCH}/{fname}','w').write(base64.b64decode('{payload}').decode())\""
    )
    rc, _, _ = run(setup, timeout=15)
    if rc != 0:
        # A partial write may still exist — remove best-effort, never
        # affect the return value.
        try:
            run(f"docker exec {cname} rm -f {HOST_SCRATCH}/{fname}", timeout=10)
        except Exception:
            pass
        return rc, ""
    try:
        rc, out = run_bytes(
            f"docker exec {cname} waydroid shell sh {DROID_SCRATCH}/{fname}",
            timeout=timeout)
    finally:
        try:
            run(f"docker exec {cname} rm -f {HOST_SCRATCH}/{fname}", timeout=10)
        except Exception:
            pass
    return rc, out.decode(errors="replace")


def check_roblox(name):
    rc, out = waydroid_exec(name, "pm list packages | grep com.roblox.client", timeout=25)
    return "com.roblox.client" in out


def _send_input_cmds(name, cmds):
    """Send input commands via FIFO (fast) or waydroid_exec (slow fallback).
    Commands are joined into ONE line per FIFO open: the input daemon does
    `read cmd < $FIFO` (reopening after every line), so separate writes can
    be lost or split. One compound write is atomic for the reader."""
    # Sink gate (defense in depth, same class as waydroid_exec's): the
    # FIFO path below is built straight from the name, and this helper
    # is reached from the auto-login watcher and the batch worker, which
    # iterate instances.json keys — a corrupted key must never drive a
    # filesystem path. HTTP routes already gate names; this covers the
    # internal callers. Fail closed: no FIFO touch, no fallback exec.
    if not valid_instance_name(name):
        return False
    cname = f"wd-{name}"
    fifo_path = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/jerusalem/input.fifo"
    compound = "; ".join(cmds)
    try:
        fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (compound + "\n").encode())
        finally:
            os.close(fd)
        return True
    except OSError:
        pass
    for cmd in cmds:
        rc, _ = waydroid_exec(name, cmd, timeout=15)
        if rc != 0:
            return False
    return True


def roblox_focused(name):
    """Is Roblox the focused app in this instance? True/False/None.

    Tri-state, mirroring the in-container keepalive's roblox_focused():
    True = dumpsys window's mCurrentFocus names com.roblox.client;
    False = the probe answered and mCurrentFocus names something else
    (a missing mCurrentFocus line on a successful probe counts as not
    focused, same as the keepalive's grep -m1 fallthrough — a healthy
    dump always carries the line);
    None = unknown (invalid name, probe error, timeout). Callers must
    NOT tap on unknown: a tap on an actually-focused session yanks the
    user out of a running game, while a skipped launch is retryable
    from the viewer Roblox button or "Roblox all". Never raises.
    """
    try:
        if not valid_instance_name(name):
            return None
        rc, out = waydroid_exec(name, "dumpsys window", timeout=15)
        if rc != 0:
            return None
        for line in (out or "").splitlines():
            if "mCurrentFocus" in line:
                return "com.roblox.client" in line
        return False
    except Exception:
        return None


def launch_roblox(name):
    """Launch Roblox by tapping its icon in the app drawer.
    Returns True if Roblox is already focused (no taps sent) or the tap
    sequence was sent; False when the focus probe is unknown or the name
    is invalid. The gate matters because openViewer() fires this on every
    viewer open and the fleet "Roblox all" job fires it on every instance:
    without it, opening the viewer on a running game sent HOME + drawer +
    icon tap and killed the session the user wanted to watch — while the
    keepalive already keeps Roblox focused on healthy instances, so the
    tap was redundant as well as destructive."""
    if not valid_instance_name(name):
        return False
    focused = roblox_focused(name)
    if focused is True:
        print(f"[launch] {name}: Roblox already focused, no taps sent",
              flush=True)
        return True
    if focused is None:
        print(f"[launch] {name}: focus probe unknown, launch skipped "
              f"(no taps sent)", flush=True)
        return False
    # Go home, open app drawer, tap Roblox icon.
    # Coordinates verified working on 1024x608 physical display.
    return _send_input_cmds(name, [
        "input keyevent 3",          # HOME
        "sleep 1",
        "input swipe 512 400 512 100",  # open app drawer
        "sleep 2",
        "input tap 129 424",         # tap Roblox icon
    ])


def _version_at_least(v, need):
    """Compare dotted version strings, e.g. _version_at_least("1.0.2", "1.0.2")."""
    try:
        def parts(s):
            return [int(x) for x in re.split(r"\D+", str(s)) if x != ""]
        return parts(v) >= parts(need)
    except Exception:
        return False


def _parse_agent_version(s):
    """Strict dotted-version parse -> tuple of ints, or None when unparseable."""
    try:
        parts = [int(x) for x in re.split(r"\D+", str(s).strip()) if x != ""]
        return tuple(parts) if parts else None
    except Exception:
        return None


def _read_agent_version_file(path):
    """Best-effort extraction of VERSION = "..." from a wing/agent.py file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return ""


_CURRENT_AGENT_VERSION = None
_CURRENT_AGENT_SHA256 = None

# Max bytes the manager will ever serve (or an agent accept) for a wing
# agent.py self-update. The file is ~120KB today; 2MB is a generous bound
# that still caps a compromised/truncated transfer.
AGENT_UPDATE_MAX_BYTES = 2 * 1024 * 1024


def _agent_disk_path():
    """Absolute path of the wing/agent.py the manager advertises + serves.

    The manager runs FLAT on the VPS (/root/jerusalem/app.py beside
    /root/jerusalem/wing/agent.py) but NESTED in the repo (manager/app.py
    with wing/agent.py at the repo root). Prefer the deployed layout --
    it is the same WING_DIR the /api/install/agent.py route serves from,
    so the advertised sha256 can never disagree with the served bytes;
    fall back to the repo layout. Every override-free caller
    (current_agent_version, agent_file_sha256, the
    /api/agent/update/download route) goes through here. Never raises.
    """
    try:
        deployed = os.path.join(WING_DIR, "agent.py")
        if os.path.exists(deployed):
            return deployed
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "wing", "agent.py")


def current_agent_version(_path=None):
    """VERSION of the wing agent deployed beside this manager.

    The manager runs flat on the VPS (/root/jerusalem/app.py beside
    /root/jerusalem/wing/agent.py) and nested in the repo (manager/app.py
    with wing/agent.py at the repo root); _agent_disk_path() resolves both,
    so the current version is read from disk, never hard-coded -- a VERSION
    bump can never go stale here. Cached per process (a deploy restarts the
    manager); "" means unknown. The _path override exists only for unit
    tests. Never raises.
    """
    global _CURRENT_AGENT_VERSION
    if _path is not None:
        return _read_agent_version_file(_path)
    if _CURRENT_AGENT_VERSION is None:
        _CURRENT_AGENT_VERSION = _read_agent_version_file(_agent_disk_path())
    return _CURRENT_AGENT_VERSION


def _read_file_sha256(path):
    """Hex sha256 of a file, or "" when missing/unreadable. Never raises."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def agent_file_sha256(_path=None):
    """Hex sha256 of the wing agent.py deployed beside this manager.

    Cached per process (a deploy restarts the manager); "" when the file is
    missing/unreadable. The _path override exists only for unit tests.
    Never raises.
    """
    global _CURRENT_AGENT_SHA256
    if _path is not None:
        return _read_file_sha256(_path)
    if _CURRENT_AGENT_SHA256 is None:
        _CURRENT_AGENT_SHA256 = _read_file_sha256(_agent_disk_path())
    return _CURRENT_AGENT_SHA256


def agent_update_directive(agent_ver):
    """{"version", "sha256"} when the node's agent is older than the one
    deployed beside this manager, else None. The agent pulls the file from
    GET /api/agent/update/download and verifies the sha256 itself.
    Never raises.
    """
    try:
        if not agent_update_available(agent_ver):
            return None
        latest = current_agent_version()
        sha = agent_file_sha256()
        if not latest or not sha:
            return None
        return {"version": latest, "sha256": sha}
    except Exception:
        return None


def agent_update_available(agent_ver, latest=None):
    """True if the node's agent is older than the deployed one, False if
    current or newer, None when either version is unknown/unparseable.
    Never raises."""
    try:
        if latest is None:
            latest = current_agent_version()
        have = _parse_agent_version(agent_ver)
        want = _parse_agent_version(latest)
        if not have or not want:
            return None
        return want > have
    except Exception:
        return None


def _wing_input(provider_meta, name, payload, timeout=15):
    """Send one input payload to a remote node's wing agent."""
    # Sink gate (defense in depth, same class as do_instance_input's):
    # the name is interpolated into the agent URL path below, and this
    # helper is reached from the auto-login watcher via the remote
    # login helper — a corrupted roblox_accounts.json key must never
    # drive an agent request. Fail closed before any agent call.
    if not valid_instance_name(name):
        return False, {"ok": False}
    code, resp = wing_request(provider_meta, "POST",
                             f"/api/agent/instances/{name}/input",
                             payload, timeout=timeout)
    ok = 200 <= code < 300
    return ok, resp if isinstance(resp, dict) else {"ok": ok}


def _remote_started_at(provider_meta, name):
    """Container StartedAt epoch for a remote instance (0 if unknown)."""
    # Sink gate (defense in depth, same class as do_instance_input's):
    # the name is interpolated into the agent URL path below, and this
    # helper is reached from the watcher pass, which iterates
    # roblox_accounts.json keys directly — no route gate stands between
    # a corrupted account key and this URL. Fail closed before any
    # agent call; 0 reads as "unknown" to the caller, exactly like a
    # probe failure.
    if not valid_instance_name(name):
        return 0
    code, st = wing_request(provider_meta, "GET",
                            f"/api/agent/instances/{name}/status", timeout=10)
    if 200 <= code < 300 and isinstance(st, dict):
        try:
            return int(st.get("started_at") or 0)
        except (TypeError, ValueError, OverflowError):
            # OverflowError: int(Infinity) from a bad agent payload.
            return 0
    return 0


def _roblox_login_remote(provider, name, username, password):
    """Roblox login sequence on a remote node via the wing agent.
    Needs agent >= 1.0.2 (text input endpoint). Sleeps run manager-side
    between stateless agent calls. Returns (ok, message)."""
    # Sink gate (defense in depth, same class as _roblox_login_inner's):
    # the name is interpolated into the agent URL paths below. The
    # login caller gates today, but this helper must fail closed on its
    # own so no future internal caller can drive agent URLs from a
    # corrupted account key. Fail closed before any provider or agent
    # call, indistinguishable from an unknown instance.
    if not valid_instance_name(name):
        return False, "instance not found"
    p = load_providers().get(provider)
    if not p:
        return False, "node not found"
    # Agent must be new enough to type text
    code, health = wing_request(p, "GET", "/api/agent/health", timeout=10)
    ver = health.get("version", "") if isinstance(health, dict) else ""
    if not (200 <= code < 300) or not _version_at_least(ver, "1.0.38"):
        return False, ("node agent too old for auto-login (needs 1.0.38+) - "
                       "re-run the node installer on that machine")
    code, st = wing_request(p, "GET", f"/api/agent/instances/{name}/status", timeout=10)
    if not (200 <= code < 300) or not (isinstance(st, dict) and st.get("running")):
        return False, "instance not running"
    # Bring Roblox to the login screen
    wing_request(p, "POST", f"/api/agent/instances/{name}/launch-roblox", timeout=30)
    time.sleep(10)
    pwd_b64 = base64.b64encode(password.encode()).decode()
    # Normalized 0-1000 coords; agent scales to 1024x608.
    # Login form fields (verified): user (512,228), pass (512,296), sign-in (512,367).
    seq = [
        ({"type": "tap", "x": 500, "y": 375}, 2),   # username field
        ({"type": "text", "text": username}, 1),
        ({"type": "tap", "x": 500, "y": 487}, 2),   # password field
        ({"type": "text", "text": pwd_b64, "b64": True}, 2),
        ({"type": "tap", "x": 500, "y": 604}, 0),   # Sign In
    ]
    for payload, nap in seq:
        ok, resp = _wing_input(p, name, payload)
        if not ok:
            return False, f"remote input failed: {resp}"
        if nap:
            time.sleep(nap)
    return True, "login sequence sent - check if login succeeded"


def _discard_pwd_file(pwd_path):
    """Best-effort removal of a staged login password + its tmp file.
    Never raises. Called on every exit path of the local login so a
    failed script write, a raised exception, or a normal completion can
    never leave a cleartext password in the instance volume."""
    for p in (pwd_path, pwd_path + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass


def _discard_login_script(script_path):
    """Best-effort removal of the staged auto_login.sh script. Never raises.
    The script carries the account username (shlex-quoted) and is only
    needed while waydroid_exec runs it; leaving it behind (the old
    behavior) kept the username in the instance volume forever, readable
    from inside Android and from the host volume path. Paired with
    _discard_pwd_file on every exit path of the local login."""
    try:
        os.remove(script_path)
    except OSError:
        pass


def sweep_stale_pwd_files(instances=None, volbase=None):
    """Remove pwd.txt + auto_login.sh (+ their .tmp siblings) +
    waydroid_exec cmd.sh leftovers
    from local instance volumes. A manager crash (kill -9, power loss)
    between the staging writes and their removal in _roblox_login_inner
    leaves a cleartext password (pwd.txt) and the account username
    (auto_login.sh, or its .tmp if the crash lands between the tmp
    write and the rename) in the volume forever — no later login overwrites
    them unless that instance logs in again, and nothing ever cleaned
    them up. The same crash window applies to waydroid_exec: its
    per-call cmd-<hex>.sh script (and the legacy shared cmd.sh from
    before waydroid-exec-cleanup) is removed after every run, but a
    crash between the write and the post-run rm leaves the most recent
    command in the volume. main() runs this sweep at startup, before
    any login thread can start, so a restart (the recovery path from
    exactly that crash) always cleans up.
    Returns the number of files removed. Best-effort, never raises;
    remote instances are skipped (their staging lives on the node),
    and names are gated so a hand-edited instances.json can never
    turn this into a path-traversal delete. (Name kept from the
    pwd.txt-only era; it now sweeps the login script and the exec
    scripts too.)"""
    if volbase is None:
        volbase = VOLUMES_BASE
    removed = 0
    try:
        if instances is None:
            instances = load_instances()
        if not isinstance(instances, dict):
            return 0
        items = list(instances.items())
    except Exception:
        return 0
    for name, info in items:
        try:
            if not valid_instance_name(name):
                continue
            if isinstance(info, dict) and info.get("provider", "local") != "local":
                continue
            jer = (f"{volbase}/wd-{name}-data/_data/data/media/0/jerusalem")
            for p in (jer + "/pwd.txt", jer + "/pwd.txt.tmp", jer + "/auto_login.sh",
                      jer + "/auto_login.sh.tmp"):
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
            # waydroid_exec staging leftovers: the legacy shared cmd.sh
            # plus per-call cmd-<hex>.sh scripts (crash between the write
            # and the post-run rm). Flat dir listing only, name-gated
            # patterns — never a recursive delete, never a symlink chase
            # beyond what os.remove does to the entry itself.
            try:
                for fn in os.listdir(jer):
                    if fn == "cmd.sh" or (fn.startswith("cmd-") and fn.endswith(".sh")):
                        try:
                            os.remove(os.path.join(jer, fn))
                            removed += 1
                        except OSError:
                            pass
            except OSError:
                pass
        except Exception:
            continue
    return removed


def sweep_stale_state_tmp(data_dir=None, journal_dir=None):
    """Remove stale atomic-write tmp files from the manager state dir.

    Every save_* writes FILE + ".tmp" then os.replace()s it into place,
    so a manager crash mid-write leaves the tmp behind forever — and
    two of them are credential-bearing: roblox_accounts.json.tmp holds
    every fleet Roblox credential in plaintext, tokens.json.tmp holds
    every dashboard session token, and static_join_token.tmp holds the
    join token. The instance-volume sweep above never looked here, and
    nothing else ever cleaned this dir: _recover_batch_jobs() skips
    non-.json names, so batch_jobs/<jid>.json.tmp files from a crash
    mid-journal were orphaned the same way.

    Removes exactly the six known state files' .tmp siblings plus
    journal files matching <16-hex-jid>.json.tmp — a flat listing with
    name-gated patterns only, never a glob, never recursive, never a
    live file (the .tmp suffix is what makes a file stale: no live
    write ever reads a .tmp, and a concurrent save_* holds its own tmp
    only between its open() and its replace() — main() runs this sweep
    before the server binds, so no save can be in flight). Returns the
    number of files removed. Best-effort, never raises."""
    removed = 0
    try:
        if data_dir is None:
            data_dir = DATA_DIR
        for f in (STATIC_TOKEN_FILE, PROVIDERS_FILE, INSTANCES_FILE,
                  ACCOUNTS_FILE, TOKENS_FILE, LOGIN_RESULTS_FILE):
            try:
                os.remove(os.path.join(data_dir, os.path.basename(f)) + ".tmp")
                removed += 1
            except OSError:
                pass
    except Exception:
        pass
    try:
        if journal_dir is None:
            journal_dir = BATCH_JOURNAL_DIR
        for fn in os.listdir(journal_dir):
            try:
                if not fn.endswith(".json.tmp"):
                    continue
                if not _BATCH_JID_RE.match(fn[:-9]):
                    continue
                os.remove(os.path.join(journal_dir, fn))
                removed += 1
            except OSError:
                pass
    except Exception:
        pass
    return removed


def _roblox_login_inner(name):
    """The login sequence itself. Returns (ok, message). Use roblox_login()
    instead — it records the outcome for dashboard visibility."""
    # Sink gate (defense in depth, same class as waydroid_exec's and
    # _send_input_cmds'): this function WRITES the cleartext password
    # file and the login script into a volume path built from the name,
    # and it is reached from the auto-login watcher and the batch-login
    # worker, which iterate instances.json keys — a corrupted key (the
    # same class the t154/t155/t156 record-shape guards were built for)
    # must never drive those writes or the tap sequence. HTTP routes
    # already gate names; this covers the internal callers. The wrapper
    # still records this outcome (record_login_result sanitizes names).
    if not valid_instance_name(name):
        return False, "instance not found"
    accounts = load_accounts()
    # account_of: a corrupt record reads as "no credentials saved"
    # instead of crashing on .get (a non-empty string is truthy, so the
    # old `not acct` guard did NOT save this site).
    acct = account_of(accounts.get(name))
    if not acct or not acct.get("username") or not acct.get("password"):
        return False, "no credentials saved for this instance"

    username = acct["username"]
    password = acct["password"]
    provider = instance_provider_of(load_instances().get(name))
    if provider is None:
        return False, "instance not found"
    if provider != "local":
        return _roblox_login_remote(provider, name, username, password)

    cname = f"wd-{name}"
    
    # Ensure Roblox is in foreground
    _send_input_cmds(name, ["input keyevent 3", "sleep 1"])
    launch_roblox(name)
    time.sleep(10)  # Wait for Roblox to load to login screen
    
    # Write password to file via base64 to avoid shell special char issues.
    # Atomic tmp+rename with 600 perms (login-pwd-cleanup): the old bare
    # open("wb") left a world-readable file and a torn write behind on a
    # crash, and the removal below only ran on the happy path — a failed
    # script write or a raised exception left the cleartext password in
    # the volume forever, with no sweep anywhere. Now every exit path
    # discards it (finally block) and main() sweeps leftovers at startup.
    pwd_b64 = base64.b64encode(password.encode()).decode()
    vol_path = f"{VOLUMES_BASE}/wd-{name}-data/_data/data/media/0/jerusalem"
    pwd_path = f"{vol_path}/pwd.txt"
    pwd_tmp = pwd_path + ".tmp"
    try:
        with open(pwd_tmp, "wb") as f:
            f.write(base64.b64decode(pwd_b64))
        os.chmod(pwd_tmp, 0o600)
        os.replace(pwd_tmp, pwd_path)
    except OSError:
        _discard_pwd_file(pwd_path)
        return False, "could not write password file"
    
    # Login sequence using verified coordinates (1024x608)
    # Username field: (512, 228), Password: (512, 296), Sign In: (512, 367)
    # Shell-quote the username for the sh script: the saved value is
    # user-typed (dashboard Account panel / bulk import) and the script runs
    # under `sh` as root in the container — $(...), backticks, ;, newlines
    # etc. would execute. Spaces stay %s-encoded for `input text`;
    # shlex.quote makes everything else inert (password is via file).
    safe_user = shlex.quote(username.replace(" ", "%s"))
    
    cmds = [
        "input tap 512 228",  # username field
        "sleep 2",
        f"input text {safe_user}",
        "sleep 1",
        "input tap 512 296",  # password field
        "sleep 2",
        'P=$(cat /data/media/0/jerusalem/pwd.txt); input text "$P"',
        "sleep 2",
        "input tap 512 367",  # Sign In button
    ]
    
    # Send via waydroid shell script (more reliable than FIFO for multi-command)
    # Atomic tmp+rename with 600 perms (login-script-600): the old bare
    # open("w") left the script world-readable in the shared media dir —
    # readable from inside Android by any app while the login ran, and
    # until the startup sweep after a manager crash mid-login. The
    # script carries the account username; the password file next to
    # it already had the tmp+chmod+replace treatment, the script did
    # not. os.replace preserves the tmp's mode, so the live file is
    # 600 from birth.
    script_content = "\n".join(cmds)
    script_path = f"{vol_path}/auto_login.sh"
    script_tmp = script_path + ".tmp"
    try:
        with open(script_tmp, "w") as f:
            f.write(script_content)
        try:
            os.chmod(script_tmp, 0o600)
        except OSError:
            pass
        os.replace(script_tmp, script_path)
    except OSError:
        # Old code returned here WITHOUT removing pwd.txt: a failed
        # script write leaked the password into the volume permanently.
        _discard_pwd_file(pwd_path)
        _discard_login_script(script_path)
        try:
            os.remove(script_tmp)
        except OSError:
            pass
        return False, "could not write login script"

    try:
        rc, out = waydroid_exec(name, "sh /data/media/0/jerusalem/auto_login.sh 2>&1", timeout=60)
    finally:
        # Happy path, timeout, or a future raising waydroid_exec: the
        # password file AND the script are gone either way. The script
        # used to be left in the volume forever after every login
        # (login-script-cleanup) — it carries the account username.
        # (Crash of the whole manager process is the one path finally
        # cannot cover — main()'s startup sweep handles that.)
        _discard_pwd_file(pwd_path)
        _discard_login_script(script_path)

    return True, "login sequence sent - check if login succeeded"


def roblox_login(name, by="manual"):
    """Automatically log into Roblox using stored credentials.
    Uses fixed coordinates for 1024x608 display (verified working).
    Works on local and remote nodes. Returns (ok, message).
    Records the outcome (no credentials) so the dashboard can show
    whether each instance's login succeeded. `by` is "auto" (watcher),
    "manual" (dashboard Login button / API), or "batch" (fleet Login all)."""
    ok, msg = _roblox_login_inner(name)
    record_login_result(name, ok, msg, by)
    return ok, msg


def check_booted(name):
    rc, out = waydroid_exec(name, "getprop sys.boot_completed", timeout=25)
    return out.strip() == "1"


def _container_started_at(cname):
    info = docker_inspect(cname)
    if not info:
        return 0
    try:
        started = info["State"]["StartedAt"]  # e.g. 2026-10-06T13:35:01.123456789Z
        import datetime
        dt = datetime.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


def _roblox_installed(name):
    rc, out = waydroid_exec(name, "pm list packages com.roblox.client", timeout=30)
    return rc == 0 and "com.roblox.client" in out


# In-flight auto-login sequences, by instance name. The watcher loop runs
# every 60s but a login sequence can take longer (waydroid_exec timeouts);
# without this guard a second sequence could start while the first is still
# tapping, interleaving two tap streams at the same login fields. The set
# is guarded by _lock; entries are removed in _auto_login_run's finally so
# a crashed sequence never blocks later attempts forever (and a manager
# restart clears the set anyway, since the threads die with the process).
_auto_login_inflight = set()


def _auto_login_run(name):
    try:
        ok, msg = roblox_login(name, by="auto")
        print(f"[auto-login] {name}: ok={ok} {msg}", flush=True)
    except Exception as e:
        print(f"[auto-login] {name} exception: {e}", flush=True)
    finally:
        try:
            with _lock:
                _auto_login_inflight.discard(name)
        except Exception:
            pass


def auto_login_watcher():
    """Background thread: auto-login to Roblox on first boot for instances
    with auto_login enabled. Bounded: first-boot window (30 min after creation)
    only, max 3 attempts. Restarts are skipped (Roblox session persists in the
    volume), the dashboard Login button covers edge cases. Works for local and
    remote-node instances.

    Success short-circuit (auto-login-success): the old loop never read
    roblox_login_results, so a first boot ALWAYS burned all 3 attempts even
    when attempt 1 succeeded — attempts 2 and 3 re-tapped the username /
    password / Sign In coordinates into whatever screen Roblox was showing
    post-login. Now a recorded ok result timestamped at/after this boot's
    start marks the boot done before any further attempt is fired (a result
    from an older boot has ts < started, so it never short-circuits a fresh
    boot). An in-flight guard (_auto_login_inflight) likewise stops the 60s
    loop from starting a second sequence while one is still tapping."""
    time.sleep(90)  # let the server and any in-flight boots settle
    while True:
        _auto_login_pass()
        time.sleep(60)


def _auto_login_pass():
    """One auto-login watcher pass, extracted so unit tests can drive it
    without the 90s/60s sleeps. Never raises (the loop must never die)."""
    try:
        accounts = load_accounts()
        instances = load_instances()
        providers = load_providers()
        login_results = load_login_results()
        for name, acct in list(accounts.items()):
            # Sink gate (defense in depth, same class as the watcher
            # name-gate): this loop iterates roblox_accounts.json keys
            # directly, and the remote branch below probes the node with
            # the raw key (the started-at helper + an inline status
            # probe) BEFORE the login sequence's own gate in
            # _roblox_login_inner ever runs. A corrupted account key
            # must never drive an agent URL or a thread spawn. The local
            # branch already fails closed indirectly (docker_inspect's
            # wd- gate makes a hostile name read as "not started"), but
            # the remote branch had no gate at all. Skip invalid keys
            # outright — they match no real instance.
            if not valid_instance_name(name):
                continue
            if not isinstance(acct, dict) or not acct.get("auto_login"):
                continue
            if not acct.get("username") or not acct.get("password"):
                continue
            inst = instances.get(name)
            if not isinstance(inst, dict):
                continue
            provider = instance_provider_of(inst)
            if provider is None:
                continue
            if provider == "local":
                cname = f"wd-{name}"
                started = _container_started_at(cname)
            else:
                p = providers.get(provider)
                if not isinstance(p, dict) or not p:
                    continue
                started = _remote_started_at(p, name)
            if not started or acct.get("login_boot") == started:
                continue
            # Already logged in on THIS boot (auto, manual, or batch):
            # mark the boot done instead of firing another sequence.
            # ts is recorded at login time, so a result from any older
            # boot is always older than this boot's start.
            lr = login_results.get(name)
            if (isinstance(lr, dict) and lr.get("ok")
                    and isinstance(lr.get("ts"), (int, float))
                    and lr["ts"] >= started):
                acct["login_boot"] = started
                save_accounts(accounts)
                continue
            created = inst.get("created_at", 0) or 0
            if started - created > 1800:
                acct["login_boot"] = started  # a restart, not first boot
                save_accounts(accounts)
                continue
            attempts = int(acct.get("login_attempts") or 0)
            if attempts >= 3:
                acct["login_boot"] = started
                save_accounts(accounts)
                continue
            if provider == "local":
                if not check_booted(name):
                    continue
                if not _roblox_installed(name):
                    continue
            else:
                # Remote: wait for boot + Roblox install via agent status
                p = providers.get(provider)
                code, st = wing_request(
                    p, "GET", f"/api/agent/instances/{name}/status", timeout=10)
                if not (200 <= code < 300 and isinstance(st, dict)
                        and st.get("setup") == "ready" and st.get("roblox")):
                    continue
            # Never overlap two sequences on one instance: a slow
            # sequence (waydroid_exec timeouts) can still be tapping
            # when the 60s loop comes around again. Skipping here burns
            # no attempt — the in-flight sequence owns this boot. The
            # guard is added only after the save, immediately before
            # the start, so no exception path between add and start
            # can leak an entry for a thread that never ran.
            with _lock:
                if name in _auto_login_inflight:
                    continue
            acct["login_attempts"] = attempts + 1
            save_accounts(accounts)
            with _lock:
                _auto_login_inflight.add(name)
            try:
                threading.Thread(target=_auto_login_run, args=(name,),
                                 daemon=True,
                                 name=f"auto-login-{name}").start()
            except Exception:
                # Thread never started: release the guard so the next
                # loop pass can retry instead of blocking this boot.
                with _lock:
                    _auto_login_inflight.discard(name)
    except Exception:
        pass


# Host CPU/RAM stats (cached briefly to avoid /proc churn)
_host_stats_cache = {"t": 0, "cpu": 0.0}
_host_stats_prev = {"idle": 0, "total": 0}

def get_host_stats():
    """Return {cpu_percent, ram_percent, ram_used_mb, ram_total_mb}."""
    global _host_stats_cache, _host_stats_prev
    now = time.time()
    # CPU: read /proc/stat twice, compute delta (use cache if <2s old)
    cpu_pct = _host_stats_cache["cpu"]
    if now - _host_stats_cache["t"] > 2:
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()[1:]
            vals = list(map(int, parts))
            idle = vals[3] + vals[4]
            total = sum(vals)
            p_idle, p_total = _host_stats_prev["idle"], _host_stats_prev["total"]
            if p_total:
                d_idle, d_total = idle - p_idle, total - p_total
                cpu_pct = round(100.0 * (1 - d_idle / d_total), 1) if d_total else 0.0
            _host_stats_prev = {"idle": idle, "total": total}
            _host_stats_cache = {"t": now, "cpu": cpu_pct}
        except Exception:
            pass
    # RAM: /proc/meminfo
    ram_pct, used_mb, total_mb = 0.0, 0, 0
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.strip().split()[0])
        total_mb = mem.get("MemTotal", 0) // 1024
        avail_mb = mem.get("MemAvailable", mem.get("MemFree", 0)) // 1024
        used_mb = total_mb - avail_mb
        ram_pct = round(100.0 * used_mb / total_mb, 1) if total_mb else 0.0
    except Exception:
        pass
    return {"cpu_percent": cpu_pct, "ram_percent": ram_pct,
            "ram_used_mb": used_mb, "ram_total_mb": total_mb}


# ---------------------------------------------------------------- WebSocket

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_handshake(headers):
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode()


def ws_send_binary(sock, data):
    header = bytes([0x82])
    n = len(data)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += struct.pack(">BH", 126, n)
    else:
        header += struct.pack(">BQ", 127, n)
    sock.sendall(header + data)


def ws_send_text(sock, text):
    """Send a UTF-8 text frame (opcode 0x1). Used for stream keepalives."""
    data = text.encode("utf-8")
    header = bytes([0x81])
    n = len(data)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += struct.pack(">BH", 126, n)
    else:
        header += struct.pack(">BQ", 127, n)
    sock.sendall(header + data)


# ws_read_frame timeout sentinel (ws-reader-timeout): both stream sockets
# run with settimeout(10) so the reader thread wakes periodically and can
# notice stop. A recv timeout is therefore NOT a close — it is the wake-up.
# Before this fix it fell into the OSError handler and returned (None, None),
# which both readers treated as "client closed", so every MJPEG/H.264 viewer
# was killed ~10s after opening (the dashboard client never sends frames on
# these sockets) and then reconnected in a loop. Timeout now returns this
# sentinel; the readers keep waiting. socket.timeout must be caught before
# OSError (it is a subclass). Caveat: a timeout mid-frame drops the partial
# frame — only a client sending partial frames then stalling >10s hits it,
# and browsers send complete frames atomically.
WS_READ_TIMEOUT = -1


def ws_read_frame(sock):
    """Read one client frame; return (opcode, payload), (None, None) on
    close/error, or (WS_READ_TIMEOUT, None) when no frame arrived before
    the socket timeout — the caller keeps waiting on a timeout."""
    try:
        hdr = sock.recv(2)
        if len(hdr) < 2:
            return None, None
        b1, b2 = hdr
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", sock.recv(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", sock.recv(8))[0]
        mask = sock.recv(4) if masked else b""
        payload = b""
        while len(payload) < ln:
            chunk = sock.recv(ln - len(payload))
            if not chunk:
                return None, None
            payload += chunk
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return opcode, payload
    except socket.timeout:
        return WS_READ_TIMEOUT, None
    except (OSError, struct.error):
        return None, None


_local_shot_cache = {}  # name -> (mtime, jpeg_bytes)

# MJPEG viewer loops break after this many seconds with zero frames (dead
# screencap loop / unreachable wing node would otherwise spin at 20Hz
# forever — up to 20 failed HTTP fetches/sec for remote instances). The
# dashboard reconnects MJPEG automatically, so recovery needs no action.
WS_FRAMELESS_TIMEOUT = 15

# MJPEG keepalive: with send-on-change below, a static screen produces zero
# binary frames, so the server sends a tiny text ping this often while the
# source is alive. It keeps the dashboard's 3s stall badge honest (a real
# stall sends nothing at all) and stops idle proxies/NATs from dropping
# the socket.
WS_PING_SECS = 1.0

# Stale-frame gate (stale-frame-gate): a frame older than this is NOT a
# live frame. The in-container screencap loop writes live.png every 0.12s
# while a viewer is attached and every 2s when idle (worst healthy cycle
# ~7s: a 5s screencap timeout + 2s idle sleep), so 10s can never gate a
# healthy loop — but a dead loop leaves live.png behind forever, and
# without this gate capture_screenshot() returned that frozen frame (via
# the mtime cache) forever: the MJPEG loop kept sending its 1s pings, the
# H.264 broadcaster kept heartbeat-encoding, and the WebRTC track kept
# re-encoding the same JPEG — so all three transports' 15s frameless
# guards never fired and the viewer showed a frozen frame labelled live
# until the watchdog's 30s stale-stream restart. With the gate, a dead
# loop yields None, the pings/fragments stop, the dashboard stall badge
# shows (~3s later), and the frameless guards close/fail over as designed.
SCREENSHOT_STALE_SECS = 10


def capture_screenshot(name):
    """Return JPEG bytes of the instance screen, or None.
    Reads directly from the docker volume (zero exec overhead).
    Sink gate (defense in depth, screenshot-path-gate): the volume path
    below is built straight from the name, and this is the shared frame
    source for the MJPEG, H.264, and WebRTC paths — a corrupted
    instances.json key (the same class the t161-t164 sink gates were
    built for) reaching it from a future internal caller would read a
    file outside its own instance directory (the fixed live.png suffix
    still allows a traversal name to pick another volume's frame, or a
    path outside /var/lib/docker/volumes entirely). The stream routes
    all gate names already; this gate makes the sink itself fail closed
    (None, indistinguishable from a missing frame) before any path is
    built or the frame cache is touched. Valid names behave identically.
    The container's entrypoint runs a persistent screencap loop writing
    to /data/media/0/jerusalem/live.png (or /droidfarm/live.png on old images)
    inside the volume. Returns None when the frame is older than
    SCREENSHOT_STALE_SECS — a stale frame is not a live frame (see above).
    """
    if not valid_instance_name(name):
        return None
    cname = f"wd-{name}"
    vol_path_new = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/jerusalem/live.png"
    vol_path_old = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/droidfarm/live.png"
    vol_path = vol_path_new if os.path.exists(vol_path_new) else vol_path_old
    try:
        # Wait briefly for a fresh frame (avoid reading a partially-written file)
        for _ in range(3):
            if os.path.exists(vol_path):
                break
            time.sleep(0.2)
        if not os.path.exists(vol_path):
            return None
        mtime = os.path.getmtime(vol_path)
        # Stale-frame gate: never serve (or cache-hit) a frozen frame.
        if time.time() - mtime > SCREENSHOT_STALE_SECS:
            return None
        cached = _local_shot_cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
        # Read with retry in case screencap is mid-write
        png = None
        for _ in range(3):
            try:
                with open(vol_path, "rb") as f:
                    data = f.read()
                if data.startswith(b"\x89PNG") and len(data) > 1000:
                    png = data
                    break
            except OSError:
                pass
            time.sleep(0.1)
        if not png:
            return None
    except Exception:
        return None
    if HAS_PIL:
        try:
            img = Image.open(_io.BytesIO(png)).convert("RGB")
            # Downscale for speed: 480px wide is plenty for dashboard viewing
            if img.width > 480:
                h = int(img.height * 480 / img.width)
                img = img.resize((480, h), Image.BILINEAR)
            buf = _io.BytesIO()
            img.save(buf, "JPEG", quality=60, optimize=True)
            data = buf.getvalue()
            _local_shot_cache[name] = (mtime, data)
            return data
        except Exception:
            return png
    return png


def ensure_cap_script(name):
    """Legacy: screencap loop is now started by the container entrypoint.
    Kept for API compatibility; does nothing."""
    pass


# ---------------------------------------------------------------- WebRTC
_webrtc_loop = None
_webrtc_pcs = {}  # pc_id -> (RTCPeerConnection, provider, instance_name)

# Abandoned WebRTC handshakes (browser tab killed mid-ICE) never fire
# connectionstatechange: without the sweep in _webrtc_create_offer, the
# adaptive-screencap viewer count — and the fast capture loop — would be
# pinned forever for a pc that never connected.
_WEBRTC_STALE_SECS = 180

# Remote screenshot cache: one wing HTTP fetch per instance per TTL window,
# shared by the MJPEG per-viewer threads, the H.264 broadcaster tick, and the
# WebRTC track. Without it, N simultaneous viewers on a remote instance cost
# N x 20 screenshot fetches/sec against the wing node (the local path is
# already mtime-cached inside capture_screenshot, so this is remote-only).
# On fetch failure the entry is DROPPED and None returned — never serve
# stale: the 15s frameless guards on all three paths must see a dead source
# as None, not as a frozen-but-"fresh" frame.
_REMOTE_SHOT_TTL = 0.1  # seconds; the in-container loop captures at ~8Hz
_remote_shots = {}  # (prov_id, name) -> [monotonic_ts, jpeg_bytes]
_remote_shots_lock = threading.Lock()


def get_remote_screenshot(wing, prov_id, name):
    """Latest remote screenshot JPEG bytes, or None.

    At most one wing HTTP fetch per (prov_id, name) per _REMOTE_SHOT_TTL;
    concurrent viewers share the cached frame. attempts=1 fail-fast, same
    as the three call sites used before (the frameless guards own retry
    at the loop level). Best-effort, never raises.

    Sink gate (defense in depth, same class as capture_screenshot's
    t165 gate): the wing URL path below is built straight from the
    name, and this is the shared remote frame source for the MJPEG,
    H.264, and WebRTC paths — the stream routes all gate names
    already, but the sink itself must fail closed (None,
    indistinguishable from a missing frame) before any cache touch or
    wing call, so a corrupted instances.json key from a future
    internal caller can never drive a wing URL or pollute the shared
    frame cache. Valid names behave identically.
    """
    if not valid_instance_name(name):
        return None
    key = (prov_id, name)
    try:
        now = time.monotonic()
        with _remote_shots_lock:
            ent = _remote_shots.get(key)
            if ent is not None and now - ent[0] < _REMOTE_SHOT_TTL:
                return ent[1]
        try:
            code, data = wing_request(wing, "GET",
                                     "/api/agent/instances/%s/screenshot" % name,
                                     timeout=10, attempts=1)
        except Exception:
            code, data = 0, None
        if code == 200 and isinstance(data, bytes) and len(data) > 1000:
            now2 = time.monotonic()
            with _remote_shots_lock:
                _remote_shots[key] = [now2, data]
                # prune entries no viewer has touched in a while (bounded
                # memory: instances are bounded, but a deleted instance's
                # entry must not linger forever)
                for k in [k for k, v in _remote_shots.items()
                          if now2 - v[0] > 60]:
                    del _remote_shots[k]
            return data
        with _remote_shots_lock:
            _remote_shots.pop(key, None)
        return None
    except Exception:
        return None


# H.264 shared broadcasters: (provider, instance) -> H264Broadcaster.
# One ffmpeg encoder per instance no matter how many viewers are watching.
_h264_broadcasters = {}


def _get_h264_broadcaster(name, prov, wing):
    """Return the shared broadcaster for an instance, creating it if needed.

    Returns None when no frame can be captured (encoder never primes).
    Dead broadcasters are dropped from the registry on next lookup.
    """
    key = (prov, name)
    with _lock:
        b = _h264_broadcasters.get(key)
        if b is not None:
            if b.running:
                return b
            del _h264_broadcasters[key]
    # Build + prime outside _lock (start() can block up to ~8s fetching
    # the first frame; must not stall other requests' auth checks).

    def frame_source():
        """Latest screenshot as (raw RGB24 bytes, w, h), or None."""
        jpeg = None
        if wing:
            # Shared remote-screenshot cache (attempts=1 fail-fast inside);
            # the broadcaster's frameless guard (15s) owns recovery — a
            # later viewer re-creates the broadcaster via the registry.
            jpeg = get_remote_screenshot(wing, prov, name)
        else:
            jpeg = capture_screenshot(name)
        if not jpeg:
            return None
        try:
            img = Image.open(_io.BytesIO(jpeg)).convert("RGB")
            w, h = img.width, img.height
            if w % 2 or h % 2:
                img = img.resize((w + (w % 2), h + (h % 2)), Image.BILINEAR)
            return img.tobytes(), img.width, img.height
        except Exception:
            return None

    nb = H264Broadcaster(frame_source, fps=20, gop=20)
    if not nb.start(timeout=8):
        return None
    with _lock:
        b = _h264_broadcasters.get(key)
        if b is not None and b.running:
            nb.stop()  # lost a create race; use the winner
            return b
        _h264_broadcasters[key] = nb
        return nb


def _stop_h264_broadcaster(name, prov):
    """Stop + drop the shared H.264 broadcaster for a deleted instance
    (delete-state-sweep). A broadcaster left behind would keep fetching
    frames for a dead container until its frameless guard fired; stopping
    it here ends connected viewers cleanly at delete time. The stop
    happens outside _lock (it joins the encoder thread, up to 5s).
    Best-effort: never raises."""
    try:
        with _lock:
            b = _h264_broadcasters.pop((prov, name), None)
    except Exception:
        return
    if b is not None:
        try:
            b.stop()
        except Exception:
            pass


def _webrtc_force_close_all():
    """Drop every registered WebRTC peer connection and its viewer mark.

    Runs on the supervisor thread from _webrtc_loop_thread_factory when the
    webrtc-loop thread is being replaced. Registry writes happen only on
    the webrtc loop thread itself (offer creation, _close_pc), which is
    dead at this point, so the registry is quiescent — no lock needed.
    Each stranded pc held one stream_viewer_open mark (opened after the
    answer SDP was set); stream_viewer_close is lock-guarded and never
    raises, so this is safe from the supervisor thread. The pc objects'
    transports are bound to the dead loop and die with it (a cross-loop
    close() is unsafe), so they are left for GC — the viewer mark, i.e.
    the fast-capture CPU burn, is the resource reclaimed here. One WARN
    line goes to the watchdog log so the dashboard Eventos modal surfaces
    it. Best-effort, never raises."""
    try:
        stranded = list(_webrtc_pcs.items())
    except Exception:
        return
    if not stranded:
        return
    dropped = 0
    for pc_id, entry in stranded:
        try:
            _webrtc_pcs.pop(pc_id, None)
        except Exception:
            pass
        # Tolerate legacy bare-pc registry values (pre-cleanup shape).
        provider, instance_name = "local", ""
        try:
            if isinstance(entry, tuple) and len(entry) == 3:
                _, provider, instance_name = entry
        except Exception:
            continue
        try:
            # A stranded mark exactly balances the stream_viewer_open its
            # offer performed; a never-opened mark decrements to the
            # clamped 0 harmlessly.
            stream_viewer_close(provider, instance_name)
            dropped += 1
        except Exception:
            pass
    try:
        _sup_log(f"WARN webrtc loop restarted with {dropped} stranded "
                 f"peer connection(s) - viewer marks dropped")
    except Exception:
        pass


def _webrtc_loop_thread_factory():
    """Factory for the supervised webrtc-loop thread (returns an unstarted
    threading.Thread, per the register_supervised contract). Always builds a
    FRESH event loop: a loop whose run_forever() returned is not trustworthy
    to re-enter (it may hold torn-down transports and half-run callbacks),
    and a closed loop can't be re-entered at all. The old loop is closed
    best-effort first (no-op when nothing is there); never raises."""
    global _webrtc_loop
    old = _webrtc_loop
    if old is not None:
        try:
            if not old.is_closed():
                old.close()
        except Exception:
            pass
    _webrtc_loop = asyncio.new_event_loop()
    loop = _webrtc_loop
    # The old loop is dead by contract (the factory only runs when the
    # registered thread is dead), so every pc still in the registry is
    # stranded: its stale-handshake sweep, connectionstatechange handler,
    # and frameless teardown were all scheduled on the dead loop and will
    # never fire. Drop them now or their adaptive-screencap viewer marks
    # pin the in-container capture loops fast forever.
    _webrtc_force_close_all()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    return threading.Thread(target=run, daemon=True, name="webrtc-loop")


def webrtc_init():
    """Start the asyncio event loop for WebRTC in a background thread."""
    if not HAS_AIORTC:
        return
    _webrtc_loop_thread_factory().start()


def _get_webrtc_frame(name, provider):
    """Get raw RGB frame bytes (width, height, rgb_bytes) for WebRTC encoding."""
    jpeg = None
    if provider == "local":
        jpeg = capture_screenshot(name)
    else:
        wing = load_providers().get(provider)
        if wing:
            # Shared remote-screenshot cache (attempts=1 fail-fast
            # inside); the track's recv loop re-runs every frame
            # interval, which is the retry policy here.
            jpeg = get_remote_screenshot(wing, provider, name)
    if not jpeg:
        return None
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        # H.264 needs even dimensions
        w = img.width - (img.width % 2)
        h = img.height - (img.height % 2)
        if w != img.width or h != img.height:
            img = img.resize((w, h), Image.BILINEAR)
        return (img.width, img.height, img.tobytes())
    except Exception:
        return None


if HAS_AIORTC:
    class ScreenVideoTrack(VideoStreamTrack):
        """VideoStreamTrack fed by instance screenshots."""

        def __init__(self, instance_name, provider, on_frameless=None):
            super().__init__()
            self.instance_name = instance_name
            self.provider = provider
            # on_frameless: sync callable fired once when no fresh frame
            # has arrived for WS_FRAMELESS_TIMEOUT (same 15s as the
            # MJPEG/H.264 frameless guards). Without it a dead frame
            # source (dead screencap loop, wedged Waydroid session,
            # unreachable wing node) replays the last frame forever and —
            # WebRTC being first in the viewer chain — the browser stays
            # frozen instead of failing over to H.264/MJPEG.
            self._on_frameless = on_frameless
            self._last_rgb = None
            self._last_fresh = None  # monotonic clock, starts at first recv
            self._frameless_fired = False

        def _check_frameless(self):
            if (not self._frameless_fired and self._last_fresh is not None
                    and time.monotonic() - self._last_fresh
                    >= WS_FRAMELESS_TIMEOUT):
                self._frameless_fired = True
                print(f"[webrtc] {self.instance_name}: no frames for "
                      f"{WS_FRAMELESS_TIMEOUT}s, closing pc", flush=True)
                cb = self._on_frameless
                if cb is not None:
                    try:
                        cb()
                    except Exception:
                        pass

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            if self._last_fresh is None:
                # Clock starts when aiortc actually pulls frames (post-ICE),
                # not at track creation — a slow handshake must not trip it.
                self._last_fresh = time.monotonic()
            # Run blocking screenshot fetch in executor
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, _get_webrtc_frame, self.instance_name, self.provider)
            if result:
                w, h, rgb = result
                self._last_rgb = (w, h, rgb)
                self._last_fresh = time.monotonic()
            elif not self._last_rgb:
                self._check_frameless()
                await asyncio.sleep(0.1)
                return await self.recv()
            else:
                w, h, rgb = self._last_rgb
                self._check_frameless()
            frame = VideoFrame.from_ndarray(
                np.frombuffer(rgb, dtype=np.uint8).reshape(h, w, 3),
                format="rgb24")
            frame.pts = pts
            frame.time_base = time_base
            return frame


async def _webrtc_create_offer(instance_name, provider, offer_sdp, input_handler):
    """Create peer connection, return answer SDP."""
    pc = RTCPeerConnection()
    pc_id = secrets.token_hex(8)
    _webrtc_pcs[pc_id] = (pc, provider, instance_name)
    _closed = {"done": False}

    async def _close_pc(reason):
        """Idempotent teardown: pops the registry, drops the
        adaptive-screencap viewer mark, closes the pc. One close per
        open, never raises."""
        if _closed["done"]:
            return
        _closed["done"] = True
        _webrtc_pcs.pop(pc_id, None)
        stream_viewer_close(provider, instance_name)
        try:
            await pc.close()
        except Exception:
            pass

    @pc.on("connectionstatechange")
    async def on_state():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await _close_pc(pc.connectionState)

    def _stale_sweep():
        # A pc that never leaves new/connecting fired no state change and
        # would pin the viewer mark (fast capture loop) forever.
        if pc.connectionState in ("new", "connecting"):
            asyncio.ensure_future(_close_pc("stale-handshake"))

    asyncio.get_running_loop().call_later(_WEBRTC_STALE_SECS, _stale_sweep)

    # Video track
    if HAS_AIORTC:
        def _fire_frameless():
            # Runs on the webrtc loop inside track recv(); schedule the
            # idempotent teardown — never raises, never blocks the track.
            asyncio.ensure_future(_close_pc("webrtc-frameless"))

        pc.addTrack(ScreenVideoTrack(instance_name, provider,
                                     on_frameless=_fire_frameless))

    # Data channel for input (created by browser)
    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(msg):
            try:
                data = json.loads(msg) if isinstance(msg, str) else {}
                # Forward to input handler in a thread (sync context)
                threading.Thread(
                    target=input_handler,
                    args=(instance_name, provider, data),
                    daemon=True).start()
            except Exception:
                pass

    try:
        offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception:
        # Handshake failed (garbage SDP, failed answer): reap the peer
        # connection NOW. Without this, the pc (threads, UDP sockets) and
        # its _webrtc_pcs entry sat until the 180s stale sweep — up to 3
        # minutes of leaked resources per failed offer. _close_pc is
        # idempotent, so a concurrent connectionstatechange cleanup is
        # harmless; stream_viewer_close clamps at 0 because the viewer mark
        # is only opened after setLocalDescription succeeds.
        await _close_pc("handshake-failed")
        raise
    # Adaptive screencap: a WebRTC viewer keeps the in-container capture
    # loop fast too (the loop feeds live.png directly, so the MJPEG/H.264
    # hooks never saw it — without this, a WebRTC-only viewer got 2s
    # frames). Remote is a no-op here: the agent owns its own marker via
    # screenshot-fetch traffic. Open only after the answer is set, so a
    # failed handshake never leaks a viewer mark.
    stream_viewer_open(provider, instance_name)
    return pc.localDescription.sdp


def webrtc_offer_sync(instance_name, provider, offer_sdp, input_handler):
    """Sync wrapper: create WebRTC offer answer (called from HTTP handler)."""
    if not HAS_AIORTC or _webrtc_loop is None:
        return None
    # A dead loop thread (see the webrtc-loop supervisor registration in
    # main()) would otherwise hang every offer on the future below until the
    # 15s timeout; fail fast so the dashboard falls over to H.264/MJPEG
    # immediately while the supervisor restarts the loop. is_running() is a
    # plain flag read, safe to call from any thread.
    try:
        if not _webrtc_loop.is_running():
            return None
    except Exception:
        return None
    # The submit itself is inside the try: the loop can die between the
    # is_running() check above and this call (run_coroutine_threadsafe
    # raises RuntimeError on a closed loop), and an offer must fail fast
    # to the dashboard's H.264/MJPEG fallback, never escape as a 500.
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _webrtc_create_offer(instance_name, provider, offer_sdp, input_handler),
            _webrtc_loop)
        return fut.result(timeout=15)
    except Exception:
        return None


def do_instance_input(name, provider, data):
    """Execute an input command. Returns (ok_bool, result_dict).
    Usable from HTTP handler and WebRTC data channel."""
    # Sink gate (defense in depth, input-sink-gate): the HTTP input route
    # and the WebRTC offer route both gate names, but this shared core is
    # also reached from the WebRTC data channel (webrtc_input_handler),
    # whose instance name comes from the _webrtc_pcs registry rather than
    # a freshly-gated route capture. The local branch only failed closed
    # indirectly — container_status() -> docker_inspect()'s wd- gate made
    # a hostile name read as "not running" before the FIFO path was
    # opened — and the remote branch had NO gate at all: the name went
    # straight into the wing_request URL path. Fail closed at the top,
    # before any wing call, FIFO open, or waydroid_exec fallback, with
    # "instance not found" (indistinguishable from an unknown instance).
    # Valid names behave identically.
    if not valid_instance_name(name):
        return False, {"error": "instance not found"}
    itype = data.get("type")
    if provider != "local":
        p = load_providers().get(provider)
        if not isinstance(p, dict) or not p:
            return False, {"error": "node not found"}
        code, resp = wing_request(p, "POST", f"/api/agent/instances/{name}/input",
                                 data, timeout=15)
        return (200 <= code < 300), resp if isinstance(resp, dict) else {"error": "wing failed"}
    cname = f"wd-{name}"
    if container_status(cname) != "running":
        return False, {"error": "instance not running"}

    def scale(x, y):
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            return None
        # Viewer contract is 0..1000 per axis (the dashboard clamps
        # before sending). Reject non-finite coords (json.loads accepts
        # Infinity/NaN, and int(inf) raises OverflowError — which the old
        # except clause missed, so a crafted tap dropped the request with
        # no response instead of a clean 400) and out-of-range coords
        # alike: NaN fails every comparison, inf fails the upper bound.
        if not (0 <= fx <= 1000 and 0 <= fy <= 1000):
            return None
        return int(fx / 1000 * DISP_W), int(fy / 1000 * DISP_H)

    cmd = None
    if itype == "tap":
        p = scale(data.get("x"), data.get("y"))
        if not p:
            return False, {"error": "x/y required"}
        cmd = f"input tap {p[0]} {p[1]}"
    elif itype == "swipe":
        p1 = scale(data.get("x1"), data.get("y1"))
        p2 = scale(data.get("x2"), data.get("y2"))
        if not p1 or not p2:
            return False, {"error": "x1/y1/x2/y2 required"}
        cmd = f"input swipe {p1[0]} {p1[1]} {p2[0]} {p2[1]}"
    elif itype == "key":
        keys = {"HOME": 3, "BACK": 4, "APP_SWITCH": 187, "POWER": 26,
                "VOLUME_UP": 24, "VOLUME_DOWN": 25}
        code = keys.get(str(data.get("key", "")).upper())
        if code is None:
            return False, {"error": "unknown key"}
        cmd = f"input keyevent {code}"
    elif itype == "text":
        txt = str(data.get("text", ""))[:200].replace(" ", "%s")
        txt = re.sub(r"[^a-zA-Z0-9%s.,!?@_-]", "", txt)
        if not txt:
            return False, {"error": "text required"}
        cmd = f"input text {txt}"
    else:
        return False, {"error": "unknown input type"}

    # Write to input FIFO via volume (instant, no waydroid shell startup delay).
    fifo_path = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/jerusalem/input.fifo"
    try:
        fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (cmd + "\n").encode())
        finally:
            os.close(fd)
        return True, {"ok": True}
    except OSError:
        rc, _ = waydroid_exec(name, cmd, timeout=20)
        if rc != 0:
            return False, {"error": "input failed"}
        return True, {"ok": True}


def webrtc_input_handler(instance_name, provider, data):
    """Input handler for WebRTC data channel messages."""
    try:
        do_instance_input(instance_name, provider, data)
    except Exception:
        pass

# ---------------------------------------------------------------- fleet batch ops
# One async job runs one action (start/stop/restart/launch-roblox/roblox-login)
# across many instances, sequentially (no thundering herd on the 8-vCPU host).
# The dashboard polls GET /api/instances/batch/<job_id> for progress.

BATCH_ACTIONS = ("start", "stop", "restart", "launch-roblox", "roblox-login")
BATCH_TTL = 900  # job records expire 15 min after creation
_batch_jobs = {}  # job_id -> {job_id, action, names, total, done, results, status, created}


# ---- batch job journal: survive manager restarts ----
# Fleet jobs used to live only in memory: a manager restart mid-job
# (watchdog v5 self-restart, a deploy restart, a crash) silently killed the
# job — the dashboard's poll then 404'd with a cryptic error and nobody
# knew which of the 20 instances had already been processed. Every job is
# now journaled to disk after each completed instance; at startup,
# _recover_batch_jobs() re-registers journaled jobs, marking any that were
# still "running" as "interrupted" so the dashboard shows exactly what got
# done. Journal entries contain only names/actions/results — never
# credentials (passwords live in roblox_accounts.json, not here).
BATCH_JOURNAL_DIR = os.path.join(DATA_DIR, "batch_jobs")
_BATCH_JID_RE = re.compile(r"^[0-9a-f]{16}$")


def _batch_journal_path(jid):
    """Journal file for a job id, or None for a malformed id."""
    jid = str(jid or "")
    if not _BATCH_JID_RE.match(jid):
        return None
    return os.path.join(BATCH_JOURNAL_DIR, jid + ".json")


def _journal_batch_job(job):
    """Atomically persist one batch job's progress (600 perms).
    Best-effort, never raises — a journal failure degrades to the old
    in-memory behavior, never breaks a running job."""
    try:
        path = _batch_journal_path(job.get("job_id"))
        if not path:
            return
        os.makedirs(BATCH_JOURNAL_DIR, mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(job, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        pass


def _unjournal_batch_job(jid):
    """Remove a job's journal file. Best-effort, never raises."""
    try:
        path = _batch_journal_path(jid)
        if path:
            os.unlink(path)
    except Exception:
        pass


def _recover_batch_jobs():
    """Re-register journaled jobs at startup. Any job that was still
    "running" died with the old manager process -> marked "interrupted"
    (reason recorded; the dashboard shows completed vs pending names).
    Finished ("done") jobs are re-registered so their results stay
    pollable until the normal TTL sweep expires them. Never raises."""
    try:
        names = os.listdir(BATCH_JOURNAL_DIR)
    except OSError:
        return
    for fn in sorted(names):
        try:
            if not fn.endswith(".json"):
                continue
            jid = fn[:-5]
            if not _BATCH_JID_RE.match(jid):
                continue
            with open(os.path.join(BATCH_JOURNAL_DIR, fn)) as f:
                job = json.load(f)
            if not isinstance(job, dict) or job.get("job_id") != jid:
                continue
            if job.get("status") == "done":
                job["status"] = "done"
            else:
                job["status"] = "interrupted"
                job["interrupted_reason"] = "manager restarted"
            job.setdefault("results", [])
            job.setdefault("done", len(job["results"]))
            job.setdefault("total", job["done"])
            job.setdefault("created", time.time())
            with _lock:
                _batch_jobs[jid] = job
        except Exception:
            continue


def instance_single_action(name, action):
    """Run one fleet action on one instance. Returns (http_code, dict).
    Shared by the per-instance endpoints and the batch worker, so behavior
    stays identical in both paths."""
    # Sink gate (defense in depth, same class as create/resize sink gates):
    # the routes gate the name, but this core is also reached from the batch
    # worker, whose names are validated only by membership in instances.json
    # — a hand-edited or corrupted state key (e.g. "rbx1;id>/tmp/p") would
    # otherwise flow verbatim into the shell `docker start/stop` commands
    # below (and into the wing_request URL path on the remote branch).
    # Fail closed before any docker call or state read; 404 keeps a hostile
    # key indistinguishable from an unknown instance.
    if not valid_instance_name(name):
        return 404, {"error": "instance not found"}
    instances = load_instances()
    if name not in instances:
        return 404, {"error": "instance not found"}
    prov = instance_provider_of(instances.get(name))
    if prov is None:
        return 404, {"error": "instance not found"}
    if action == "roblox-login":
        # Manager-driven login (local + remote both run through roblox_login,
        # which routes by provider and records the outcome for the dashboard).
        # NOT proxied to the wing agent — there is no agent-side login endpoint.
        if prov == "local" and container_status(f"wd-{name}") != "running":
            return 409, {"error": "instance not running"}
        ok, msg = roblox_login(name, by="batch")
        return (200 if ok else 400), {"ok": ok, "msg": msg}
    if prov != "local":
        p = load_providers().get(prov)
        if not isinstance(p, dict) or not p:
            return 404, {"error": "node not found"}
        timeout = 30 if action == "launch-roblox" else 60
        if action == "restart":
            code, resp = wing_request(p, "POST",
                                     f"/api/agent/instances/{name}/stop", timeout=60)
            if code != 200:
                return code, resp if isinstance(resp, dict) else {"error": "wing failed"}
            code, resp = wing_request(p, "POST",
                                     f"/api/agent/instances/{name}/start", timeout=60)
            invalidate_instance_snapshot(name)
            if code == 200:
                # Optimistic patch: the next poll's fail-fast fetch may fail
                # and would otherwise serve the pre-restart snapshot for up
                # to _REMOTE_STATUS_TTL seconds.
                patch_remote_status(prov, name, True)
            return code, resp if isinstance(resp, dict) else {"error": "wing failed"}
        code, resp = wing_request(p, "POST",
                                 f"/api/agent/instances/{name}/{action}", timeout=timeout)
        invalidate_instance_snapshot(name)
        if code == 200 and action in ("start", "stop"):
            # launch-roblox / roblox-login don't change container state.
            patch_remote_status(prov, name, action == "start")
        return code, resp if isinstance(resp, dict) else {"error": "wing failed"}
    cname = f"wd-{name}"
    if action in ("start", "stop"):
        if action == "start":
            # A post-reboot start with a stock binderfs/loop pool wedges the
            # container (docker stubs the vanished bind sources). Best-effort
            # and guarded: a failure here must never block the start itself;
            # the watchdog owns any resulting wedge.
            try:
                _prepare_pools_for_start()
            except Exception:
                pass
        rc, _, err = run(f"docker {'start' if action == 'start' else 'stop'} {cname}",
                         timeout=60)
        if rc != 0:
            return 500, {"error": err.strip() or "docker failed"}
        invalidate_instance_snapshot(name)
        return 200, {"ok": True, "status": container_status(cname)}
    if action == "restart":
        rc, _, err = run(f"docker stop {cname}", timeout=60)
        if rc != 0:
            return 500, {"error": err.strip() or "docker failed"}
        try:
            _prepare_pools_for_start()  # best-effort, guarded (see above)
        except Exception:
            pass
        rc, _, err = run(f"docker start {cname}", timeout=60)
        if rc != 0:
            return 500, {"error": err.strip() or "docker failed"}
        invalidate_instance_snapshot(name)
        return 200, {"ok": True, "status": container_status(cname)}
    if action == "launch-roblox":
        if container_status(cname) != "running":
            return 409, {"error": "instance not running"}
        if not check_roblox(name):
            return 404, {"error": "roblox not installed"}
        ok = launch_roblox(name)
        return 200, {"ok": ok}
    return 400, {"error": "unknown action"}


def _sweep_batch_jobs():
    now = time.time()
    with _lock:
        # Never sweep a RUNNING job: a long fleet job (e.g. "Login all" on
        # 20 instances) can legitimately outlive the 15-min TTL, and the
        # worker exits early the moment its job leaves the registry — the
        # sweep used to silently kill such a job mid-flight (partial work
        # done, journal deleted, dashboard left 404ing).
        for jid in [jid for jid, job in _batch_jobs.items()
                    if now - job.get("created", now) > BATCH_TTL
                    and job.get("status") != "running"]:
            _batch_jobs.pop(jid, None)
            _unjournal_batch_job(jid)


def _mark_job_interrupted(jid, reason):
    """Mark a live job interrupted after its worker thread died.

    A dead worker thread leaves the job "running" forever, which wedges
    the one-job-at-a-time fleet-bar guard (every fleet action 409s) until
    a manager restart — and the startup-only batch-journal recovery never
    fires in a live manager. Marking it interrupted frees the guard and
    makes the dashboard's existing "Reanudar" (resume) path work on the
    never-attempted instances. Best-effort, never raises."""
    try:
        with _lock:
            job = _batch_jobs.get(jid)
            if job and job.get("status") == "running":
                job["status"] = "interrupted"
                job["interrupted_reason"] = str(reason or "worker error")[:160]
                _journal_batch_job(job)
    except Exception:
        pass


def batch_create_job(action, names):
    """Validate and start a batch job. Returns (http_code, dict)."""
    if action not in BATCH_ACTIONS:
        return 400, {"error": "action must be one of: " + ", ".join(BATCH_ACTIONS)}
    if names and not isinstance(names, list):
        # A non-list `names` (int, dict, string) used to reach
        # `[str(n) for n in names]` below and raise TypeError on iteration
        # (or iterate dict keys / string chars), killing the handler
        # thread with no response. Falsy values still mean "all instances".
        return 400, {"error": "names must be a list"}
    with _lock:
        instances = load_instances()
        targets = sorted(instances.keys()) if not names else [str(n) for n in names]
        unknown = [n for n in targets if n not in instances]
        if unknown:
            return 400, {"error": "unknown instance(s): " + ", ".join(unknown[:10])}
        if any(j.get("status") == "running" for j in _batch_jobs.values()):
            return 409, {"error": "a batch job is already running"}
    _sweep_batch_jobs()
    jid = secrets.token_hex(8)
    job = {"job_id": jid, "action": action, "names": targets,
           "total": len(targets), "done": 0, "results": [],
           "status": "running", "created": time.time()}
    with _lock:
        _batch_jobs[jid] = job
        _journal_batch_job(job)
    threading.Thread(target=_run_batch_job, args=(jid,), daemon=True,
                     name=f"batch-{jid}").start()
    return 202, {"job_id": jid, "action": action, "total": len(targets)}


def _run_batch_job(jid):
    # Last-resort thread net: the per-instance try/except covers one
    # instance's action, but an exception escaping the registry/journal
    # bookkeeping would otherwise kill this thread and leave the job
    # "running" forever (wedging the fleet-bar 409 guard). BaseException
    # so even SystemExit/KeyboardInterrupt mark the job instead of
    # stranding it.
    try:
        with _lock:
            job = _batch_jobs.get(jid)
            names = list(job["names"]) if job else []
            action = job["action"] if job else None
        for name in names:
            try:
                code, resp = instance_single_action(name, action)
                ok = code == 200 and bool(resp.get("ok", True))
                err = None if ok else (resp.get("error") or f"action failed (HTTP {code})")
            except Exception as e:  # never let one instance kill the batch
                ok, err = False, f"exception: {type(e).__name__}"
            with _lock:
                job = _batch_jobs.get(jid)
                if not job:
                    return
                job["results"].append({"name": name, "ok": ok, "error": err})
                job["done"] += 1
                _journal_batch_job(job)  # progress survives a manager restart
        with _lock:
            job = _batch_jobs.get(jid)
            if job:
                job["status"] = "done"
                _journal_batch_job(job)
    except BaseException as e:
        _mark_job_interrupted(jid, "worker error: " + type(e).__name__)


def batch_get_job(jid):
    with _lock:
        job = _batch_jobs.get(jid)
        if not job:
            return 404, {"error": "job not found"}
        view = {k: job[k] for k in ("job_id", "action", "status", "total", "done", "created")
                if k in job}
        if job.get("interrupted_reason"):
            view["interrupted_reason"] = job["interrupted_reason"]
        if "names" in job:  # create-fleet jobs show the planned names
            view["names"] = list(job["names"])
        view["results"] = list(job["results"])
        view["failed"] = [r["name"] for r in job["results"] if not r["ok"]]
    return 200, view


def batch_delete_job(jid):
    with _lock:
        job = _batch_jobs.get(jid)
        if not job:
            return 404, {"error": "job not found"}
        if job.get("status") == "running":
            return 409, {"error": "job still running"}
        _batch_jobs.pop(jid, None)
        _unjournal_batch_job(jid)
    return 200, {"ok": True}


# ---------------------------------------------------------------- fleet bulk-create
# "Create fleet" in the dashboard: create N instances named <prefix><i>
# (e.g. rbx21..rbx25) as one async job, sequentially (Waydroid image pulls
# and binder allocation are heavy; no thundering herd on the 8-vCPU host).
# A dry-run plan endpoint shows the names + RAM/binder headroom BEFORE any
# instance is created, so overprovisioning is a conscious choice, not a
# surprise. Jobs live in the same _batch_jobs registry as the fleet action
# jobs, so the dashboard polls GET /api/instances/batch/<job_id> unchanged.
# The per-instance create cores are shared with the single-create endpoint
# so behavior stays identical in both paths.

FLEET_CREATE_MAX = 20
FLEET_BINDER_DEVICE_CAP = 64  # ensure_binder_devices() ceiling; ~21 triplets

IMAGE_MISSING_ERROR = (
    "waydroid-instance image not built on this host — "
    "build the Waydroid image first (pending-deploy/waydroid-image-build), "
    "then create the instance"
)


def waydroid_image_present():
    """Is the instance image available to local docker? True/False/None.

    None = could not determine (docker daemon down, unexpected output):
    fail-open so the real `docker run` error surfaces instead of a false
    refusal. Only a positive "No such image" answer refuses the create.
    Best-effort: never raises."""
    try:
        rc, _out, err = run(f"docker image inspect {IMAGE}", timeout=15)
        if rc == 0:
            return True
        if "no such image" in (err or "").lower():
            return False
        return None
    except Exception:
        return None


def create_local_instance(name, cpus, memory_mb, ip=None):
    """Create one instance on the main node. Returns (http_code, dict).
    Shared by the single-create endpoint and the fleet-create worker."""
    # Sink gate (defense in depth, create-sink-gate): name, cpus and
    # memory_mb are interpolated into the shell `docker run` command
    # below, and the ip gate further down covers the fourth
    # interpolation. Both live callers validate today (the single-create
    # route coerces + bounds, _fleet_validate does the same), but the
    # sink itself never did — docker_inspect's gate only makes a hostile
    # name look "missing", it does not stop the create, so a future
    # internal caller passing a hostile name or a string cpus/memory
    # would reach the shell verbatim. Fail closed here, before any
    # reservation, docker call, or state write. Bounds match the route
    # and fleet validators exactly (cpus 0.5-32, memory 1024-32768 MB);
    # the coerced values are what the reservation and docker run see,
    # which is identical for both live callers (they already coerce).
    if not valid_instance_name(name):
        return 404, {"error": "instance not found"}
    try:
        cpus = float(cpus)
        memory_mb = int(memory_mb)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(Infinity) / float(huge int) from json.loads,
        # same class as the resize validator's note.
        return 400, {"error": "invalid cpus/memory"}
    if not (0.5 <= cpus <= 32) or not (1024 <= memory_mb <= 32768):
        return 400, {"error": "invalid cpus/memory"}
    with _lock:
        instances = load_instances()
        if name in instances or docker_inspect(f"wd-{name}"):
            return 409, {"error": "instance already exists"}
        ip = (str(ip or "").strip()) or next_free_ip(instances)
        if not ip:
            return 500, {"error": "no free IP"}
        binders = free_binder_triplet(used_binders(instances))
        if not binders:
            # Boot state may have dropped back to the stock device set
            # (e.g. after a host reboot); recreate the pool and retry once.
            if ensure_binder_devices():
                binders = free_binder_triplet(used_binders(instances))
        if not binders:
            return 500, {"error": "no free binder devices"}
        instance_id = next_instance_id(instances)
        # Reserve immediately
        instances[name] = {
            "name": name, "ip": ip, "cpus": cpus, "memory_mb": memory_mb,
            "binders": binders, "instance_id": instance_id,
            "provider": "local",
            "created_at": int(time.time()),
        }
        save_instances(instances)

    if not valid_ipv4(ip):
        # Fail fast: the ip is interpolated into the container launch
        # command (shell) built below. next_free_ip() only ever yields
        # dotted quads, so a failure here is a hostile user-supplied
        # override — roll back the reservation like the other fail-fast
        # paths. (Placed outside the first `with _lock:` block: the lock is
        # not reentrant, so the rollback's own `with _lock:` would
        # deadlock inside it.)
        with _lock:
            instances = load_instances()
            instances.pop(name, None)
            save_instances(instances)
        return 400, {"error": "invalid IP (dotted-quad IPv4 only)"}

    if waydroid_image_present() is False:
        # Fail fast with an actionable error instead of a cryptic
        # `docker run` failure. Reservation is rolled back like the
        # docker-run-failure path below.
        with _lock:
            instances = load_instances()
            instances.pop(name, None)
            save_instances(instances)
        return 500, {"error": IMAGE_MISSING_ERROR}

    loop_warning = ""
    if not ensure_loop_devices():
        # Fail-open (not fail-closed like binder): loop devices are consumed
        # at container boot, not reserved at create — a small fleet runs fine
        # on the stock set. The warning is loud and actionable so the host
        # gets fixed (install.sh step 4 / verify-binder.sh) before scaling.
        n = count_loop_devices()
        loop_warning = (
            "only %d loop devices on this host (fleet floor %d) — "
            "Waydroid instances past a few may fail to boot; fix the host "
            "config (install.sh step 4) or run "
            "pending-deploy/binder-verify/verify-binder.sh" %
            (n, FLEET_LOOP_DEVICE_FLOOR))
        print("[create] WARNING %s: %s" % (name, loop_warning), flush=True)

    cname = f"wd-{name}"
    b0, b1, b2 = binders
    # Note: binder devices use -v bind mounts (not --device) because
    # --device does not work correctly with binderfs devices.
    cmd = (
        f"docker run -d --name {cname} --privileged "
        f"--network {NETWORK} --ip {ip} "
        f"--cpus {cpus} --memory {memory_mb}m "
        f"-e INSTANCE_ID={instance_id} "
        f"-v /dev/binderfs/{b0}:/dev/binder "
        f"-v /dev/binderfs/{b1}:/dev/vndbinder "
        f"-v /dev/binderfs/{b2}:/dev/hwbinder "
        f"-v {cname}-varlib:/var/lib/waydroid "
        f"-v {cname}-data:/root/.local/share/waydroid "
        f"{IMAGE}"
    )
    rc, out, err = run(cmd, timeout=60)
    if rc != 0:
        with _lock:
            instances = load_instances()
            instances.pop(name, None)
            save_instances(instances)
        return 500, {"error": f"docker run failed: {(err or out).strip()[:300]}"}
    resp = {"ok": True, "name": name, "ip": ip,
            "binders": binders, "instance_id": instance_id}
    if loop_warning:
        resp["warning"] = loop_warning
    return 200, resp


def create_remote_instance(name, cpus, memory_mb, provider_id):
    """Create one instance on a wing node via the agent. Returns (http_code, dict).
    Shared by the single-create endpoint and the fleet-create worker."""
    p = load_providers().get(provider_id)
    # A non-dict record (or an empty one) is "no such node", not a
    # crash: the old code passed a truthy string record through `if not
    # p`, then raised AttributeError on p.get("last_seen") and killed
    # the create handler with no response. A record that cannot prove
    # recency is offline, never online.
    if not isinstance(p, dict) or not p:
        return 404, {"error": "node not found"}
    if time.time() - provider_last_seen(p) > 120:
        return 409, {"error": "node offline"}
    with _lock:
        instances = load_instances()
        if name in instances:
            return 409, {"error": "instance already exists"}
    # pick a free IP on that provider
    code, resp = wing_request(p, "GET", "/api/agent/instances", timeout=20)
    used_ips = set()
    if code == 200 and isinstance(resp, dict):
        # An agent returning a dict (not a list) for "instances" used to
        # iterate its string keys, and a string item crashed on .get —
        # both dropped the create request. Only a list of dicts counts.
        agent_instances = resp.get("instances")
        if isinstance(agent_instances, list):
            for i in agent_instances:
                if isinstance(i, dict) and i.get("ip"):
                    used_ips.add(i["ip"])
    ip = None
    for n in range(11, 250):
        cand = f"10.20.0.{n}"
        if cand not in used_ips:
            ip = cand
            break
    if not ip:
        return 500, {"error": "no free IP on node"}
    code, resp = wing_request(p, "POST", "/api/agent/instances",
                             {"name": name, "cpus": cpus,
                              "memory_mb": memory_mb, "ip": ip}, timeout=120)
    if code == 200:
        with _lock:
            instances = load_instances()
            instances[name] = {"name": name, "ip": ip, "cpus": cpus,
                               "memory_mb": memory_mb, "provider": provider_id,
                               "created_at": int(time.time())}
            save_instances(instances)
        # The agent just docker-run'd the container: optimistically mark it
        # running so a flaky next poll (fail-fast fetch) can't serve a
        # pre-create "stopped" for up to _REMOTE_STATUS_TTL seconds.
        patch_remote_status(provider_id, name, True)
        return 200, {"ok": True, "name": name, "ip": ip,
                     "provider": provider_id}
    return code, resp if isinstance(resp, dict) else {"error": "wing failed"}


# Instance resource resize (live, no restart): docker update applies the new
# --memory/--cpus limits to a running (or stopped) container immediately.
# Bounds match create-time (_create_instance / _fleet_validate); names pass
# the route-name-gate before reaching here (re-gated at each sink anyway).
RESIZE_MIN_MEMORY_MB = 1024
RESIZE_MAX_MEMORY_MB = 32768
RESIZE_MIN_CPUS = 0.5
RESIZE_MAX_CPUS = 32


def validate_resize_params(data):
    """Validate a resize request body.

    Returns ({"memory_mb", "cpus"}, None) or (None, err). Each field
    optional, at least one required. Never raises."""
    try:
        body = data if isinstance(data, dict) else {}
        memory_mb = body.get("memory_mb", None)
        cpus = body.get("cpus", None)
        if memory_mb is None and cpus is None:
            return None, "memory_mb or cpus required"
        if memory_mb is not None:
            memory_mb = int(memory_mb)
            if not (RESIZE_MIN_MEMORY_MB <= memory_mb <= RESIZE_MAX_MEMORY_MB):
                return None, "memory 1024-32768 MB"
        if cpus is not None:
            cpus = float(cpus)
            if not (RESIZE_MIN_CPUS <= cpus <= RESIZE_MAX_CPUS):
                return None, "cpus 0.5-32"
        return {"memory_mb": memory_mb, "cpus": cpus}, None
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(Infinity) / float(huge int) from json.loads,
        # which accepts Infinity/NaN and arbitrary-precision ints. Without
        # it the resize handler thread died with no response (same class
        # as the t148 tap-coord bug).
        return None, "invalid cpus/memory"


def resize_local_instance(name, memory_mb, cpus):
    """Live-resize a main-node instance's CPU/RAM limits. Returns (code, dict).

    docker update needs no container restart. A memory shrink below the
    current live usage is refused (it would OOM-kill the running session);
    unknown usage fails open. instances.json is only written after docker
    accepts the new limits. Never deletes data; never restarts anything."""
    if not valid_instance_name(name):
        return 404, {"error": "instance not found"}
    # Sink gate (defense in depth, resize-sink-gate): memory_mb and cpus
    # are interpolated into the shell `docker update` command below, but
    # only the name was gated at this sink — the route validator coerces
    # + bounds them, so a future internal caller passing a hostile
    # string (or an uncoerced value, which would also be stored verbatim
    # in instances.json) would reach the shell verbatim. Coerce + bound
    # here, before any docker call or state write; bounds match the
    # route validator exactly (memory 1024-32768 MB, cpus 0.5-32).
    # None means "keep the existing limit" for that field (the route
    # validator already rejects both-None); the coerced values are what
    # the docker update, the shrink check, and the state write see,
    # which is identical for the live caller (it already coerces).
    try:
        if memory_mb is not None:
            memory_mb = int(memory_mb)
        if cpus is not None:
            cpus = float(cpus)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(Infinity) / float(huge int) from json.loads,
        # same class as the resize validator's note.
        return 400, {"error": "invalid cpus/memory"}
    if memory_mb is None and cpus is None:
        return 400, {"error": "memory_mb or cpus required"}
    if memory_mb is not None and not (RESIZE_MIN_MEMORY_MB
                                      <= memory_mb <= RESIZE_MAX_MEMORY_MB):
        return 400, {"error": "invalid cpus/memory"}
    if cpus is not None and not (RESIZE_MIN_CPUS <= cpus <= RESIZE_MAX_CPUS):
        return 400, {"error": "invalid cpus/memory"}
    with _lock:
        instances = load_instances()
        inst = instances.get(name)
        if not isinstance(inst, dict):
            return 404, {"error": "instance not found"}
        if instance_provider_of(inst) != "local":
            return 409, {"error": "not a local instance"}
        new_mem = memory_mb if memory_mb is not None else inst.get("memory_mb")
        new_cpu = cpus if cpus is not None else inst.get("cpus")
    if memory_mb is not None:
        try:
            res = instance_resources(name)
        except Exception:
            res = None
        if res:
            used = res.get("mem_used_mb")
            if used is not None and memory_mb < used:
                return 400, {"error":
                    f"new memory limit {memory_mb} MB is below current usage "
                    f"{used:.0f} MB — stop the instance or pick a larger limit"}
    args = []
    if memory_mb is not None:
        args.append(f"--memory {memory_mb}m")
    if cpus is not None:
        args.append(f"--cpus {cpus}")
    # name + numerics are sink-gated above; only coerced, in-bounds
    # values reach this shell command.
    rc, out, err = run(f"docker update {' '.join(args)} wd-{name}", timeout=60)
    if rc != 0:
        return 500, {"error": f"docker update failed: {(err or out).strip()[:300]}"}
    with _lock:
        instances = load_instances()
        inst = instances.get(name)
        if isinstance(inst, dict):
            if memory_mb is not None:
                inst["memory_mb"] = memory_mb
            if cpus is not None:
                inst["cpus"] = cpus
            save_instances(instances)
    invalidate_instance_snapshot(name)
    return 200, {"ok": True, "name": name,
                 "memory_mb": new_mem, "cpus": new_cpu}


def resize_remote_instance(name, memory_mb, cpus, provider_id):
    """Resize a wing-node instance via the agent. Returns (code, dict).

    User-initiated one-shot: default wing_request retry budget (3 attempts),
    per the wing-failfast design. instances.json is only written when the
    agent accepts the new limits."""
    # Sink gate (defense in depth, remote-resize-sink-gate): the resize
    # route validates name + numerics, but this sink builds a wing URL
    # path from `name` and sends + stores memory_mb/cpus verbatim. A
    # future internal caller passing a hostile name would drive a wing
    # URL before any route gate ever ran; uncoerced numerics would be
    # sent to the agent and stored verbatim in instances.json. Gate
    # here, before any provider lookup or wing call; bounds match the
    # route validator exactly (memory 1024-32768 MB, cpus 0.5-32). None
    # means "keep the existing limit" for that field. Identical for the
    # live caller (it already coerces). The agent's own resize route
    # also gates name + numerics, so this is the manager-side layer —
    # same class as the local resize sink gate (t163).
    if not valid_instance_name(name):
        return 404, {"error": "instance not found"}
    try:
        if memory_mb is not None:
            memory_mb = int(memory_mb)
        if cpus is not None:
            cpus = float(cpus)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(Infinity) / float(huge int) from json.loads,
        # same class as the resize validator's note.
        return 400, {"error": "invalid cpus/memory"}
    if memory_mb is None and cpus is None:
        return 400, {"error": "memory_mb or cpus required"}
    if memory_mb is not None and not (RESIZE_MIN_MEMORY_MB
                                      <= memory_mb <= RESIZE_MAX_MEMORY_MB):
        return 400, {"error": "invalid cpus/memory"}
    if cpus is not None and not (RESIZE_MIN_CPUS <= cpus <= RESIZE_MAX_CPUS):
        return 400, {"error": "invalid cpus/memory"}
    p = load_providers().get(provider_id)
    # Same record-shape rule as create_remote_instance: a non-dict
    # record is "no such node" (old code crashed on p.get), and a
    # record that cannot prove recency is offline, never online.
    if not isinstance(p, dict) or not p:
        return 404, {"error": "node not found"}
    if time.time() - provider_last_seen(p) > 120:
        return 409, {"error": "node offline"}
    with _lock:
        instances = load_instances()
        if name not in instances or not isinstance(instances.get(name), dict):
            return 404, {"error": "instance not found"}
    payload = {}
    if memory_mb is not None:
        payload["memory_mb"] = memory_mb
    if cpus is not None:
        payload["cpus"] = cpus
    code, resp = wing_request(p, "POST", f"/api/agent/instances/{name}/resize",
                             payload, timeout=60)
    if code == 200:
        with _lock:
            instances = load_instances()
            inst = instances.get(name)
            if isinstance(inst, dict):
                if memory_mb is not None:
                    inst["memory_mb"] = memory_mb
                if cpus is not None:
                    inst["cpus"] = cpus
                save_instances(instances)
        invalidate_instance_snapshot(name)
        return 200, {"ok": True, "name": name,
                     "memory_mb": (inst if isinstance(inst, dict) else {}).get("memory_mb"),
                     "cpus": (inst if isinstance(inst, dict) else {}).get("cpus"),
                     "provider": provider_id}
    return code, resp if isinstance(resp, dict) else {"error": "wing failed"}


def fleet_plan_names(instances, prefix, count):
    """Next `count` free <prefix><n> names (e.g. rbx21..rbx23)."""
    nums = []
    for n in instances:
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", n)
        if m:
            nums.append(int(m.group(1)))
    start = (max(nums) + 1) if nums else 1
    return [f"{prefix}{i}"[:32] for i in range(start, start + count)]


def _fleet_validate(prefix, count, cpus, memory_mb, provider):
    """Shared validation for plan + execute. Returns (cleaned_dict, None) or
    (None, (http_code, error_dict))."""
    prefix = re.sub(r"[^a-zA-Z0-9_-]", "", str(prefix or ""))[:24]
    if not prefix:
        return None, (400, {"error": "prefix required (a-z, 0-9, -, _)"})
    try:
        count = int(count)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(Infinity) from json.loads (see resize note).
        return None, (400, {"error": "count must be 1-20"})
    if not (1 <= count <= FLEET_CREATE_MAX):
        return None, (400, {"error": "count must be 1-20"})
    try:
        cpus = float(cpus)
        memory_mb = int(memory_mb)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(Infinity) / float(huge int) from json.loads.
        return None, (400, {"error": "invalid cpus/memory"})
    if not (0.5 <= cpus <= 32) or not (1024 <= memory_mb <= 32768):
        return None, (400, {"error": "cpus 0.5-32, memory 1024-32768 MB"})
    provider = str(provider or "auto")
    providers = load_providers()
    if provider == "auto":
        provider = pick_provider(providers, need_ram_mb=memory_mb,
                                 need_cpus=cpus) or "local"
    if provider != "local" and provider not in providers:
        return None, (400, {"error": "unknown node"})
    return {"prefix": prefix, "count": count, "cpus": cpus,
            "memory_mb": memory_mb, "provider": provider}, None


def probe_remote_headroom(hr, provider_id):
    """Fill image_present/loop_devices for a wing node from its agent.

    Agent >= 1.0.21 serves GET /api/agent/headroom (authed). Older agents
    404 -> headroom_probe "old-agent" (the plan can't verify the node; the
    warning names the installer re-run). Any other failure -> "unreachable".
    Fail-fast (timeout=5, attempts=1): the plan preview is a user-initiated
    dry run, never the 10s poll — a wedged node must not stall it.
    Best-effort, never raises.
    """
    try:
        p = load_providers().get(provider_id)
        if not isinstance(p, dict) or time.time() - p.get("last_seen", 0) > 120:
            hr["headroom_probe"] = "unreachable"
            return
        code, resp = wing_request(p, "GET", "/api/agent/headroom",
                                 timeout=5, attempts=1)
        if code == 200 and isinstance(resp, dict):
            img = resp.get("image_present")
            hr["image_present"] = (True if img is True
                                   else False if img is False else None)
            try:
                ld = resp.get("loop_devices")
                ld = int(ld) if ld is not None else None
                hr["loop_devices"] = (ld if ld is not None and 0 <= ld <= 100000
                                      else None)
            except (TypeError, ValueError):
                hr["loop_devices"] = None
        elif code == 404:
            hr["headroom_probe"] = "old-agent"
        else:
            hr["headroom_probe"] = "unreachable"
    except Exception:
        hr["headroom_probe"] = "unreachable"


def fleet_headroom(instances, provider_id, count, memory_mb, cpus=None):
    """RAM/binder/CPU cost of adding `count` instances on one node.

    Returns a dict with a human-readable `warning` ("" when it fits).
    `cpus` is the per-instance CPU limit from the fleet plan; when None
    (legacy 4-arg calls) CPU accounting is inert — no CPU warning fires.
    Best-effort: never raises, tolerates missing /proc or provider stats."""
    hr = {"host_ram_mb": 0, "current_limits_mb": 0, "new_limits_mb": 0,
          "overcommit_mb": 0,
          "host_cpus": 0, "current_cpus": 0, "new_cpus": 0,
          "cpu_overcommit": 0,
          "binder_free_triplets": None,
          "binder_device_cap": FLEET_BINDER_DEVICE_CAP, "warning": "",
          "image_present": None, "loop_devices": None,
          "headroom_probe": "ok"}
    try:
        if provider_id == "local":
            hr["host_ram_mb"] = int(get_host_stats().get("ram_total_mb", 0) or 0)
            hr["host_cpus"] = int(os.cpu_count() or 0)
            hr["current_limits_mb"] = sum(
                int(v.get("memory_mb", 0) or 0) for v in instances.values()
                if isinstance(v, dict) and v.get("provider", "local") == "local")
            hr["current_cpus"] = sum(
                int(v.get("cpus", 0) or 0) for v in instances.values()
                if isinstance(v, dict) and v.get("provider", "local") == "local")
            rc, out, _ = run("ls /dev/binderfs/ 2>/dev/null", timeout=10)
            if rc == 0:
                devs = [d for d in out.split()
                        if d == "binder" or re.fullmatch(r"binder\d+", d)]
                used = set()
                for v in instances.values():
                    if not isinstance(v, dict):
                        continue
                    binders = v.get("binders", []) or []
                    if isinstance(binders, list):
                        for b in binders:
                            used.add(b)
                hr["binder_free_triplets"] = max(
                    0, len([d for d in devs if d not in used]) // 3)
            hr["loop_devices"] = count_loop_devices()
            hr["image_present"] = waydroid_image_present()
        else:
            st = provider_stats(load_providers().get(provider_id))
            hr["host_ram_mb"] = int(st.get("ram_total_mb", 0) or 0)
            hr["host_cpus"] = int(st.get("cpu_count", 0) or 0)
            hr["current_limits_mb"] = sum(
                int(v.get("memory_mb", 0) or 0) for v in instances.values()
                if isinstance(v, dict) and v.get("provider") == provider_id)
            hr["current_cpus"] = sum(
                int(v.get("cpus", 0) or 0) for v in instances.values()
                if isinstance(v, dict) and v.get("provider") == provider_id)
            # binder allocation on wing nodes is the agent's job
            # (it self-heals the pool at create, agent 1.0.16 — no
            # plan-time accounting needed); image + loop come from the
            # agent's headroom probe (1.0.21).
            probe_remote_headroom(hr, provider_id)
        hr["new_limits_mb"] = hr["current_limits_mb"] + count * memory_mb
        # CPU accounting: cpus is the plan's per-instance limit (float, 0.5-32).
        # cpus=None = legacy callers — keep fields populated but never warn.
        add_cpus = float(cpus) if cpus is not None else 0.0
        hr["new_cpus"] = hr["current_cpus"] + count * add_cpus
        warns = []
        if hr["host_ram_mb"] > 0:
            over = hr["new_limits_mb"] - hr["host_ram_mb"]
            hr["overcommit_mb"] = max(0, over)
            if over > 0:
                warns.append(
                    f"memory limits would exceed host RAM by {over} MB "
                    f"({hr['new_limits_mb']} MB limits on {hr['host_ram_mb']} MB host). "
                    "Actual use is lower, but watch for OOM kills.")
        if cpus is not None and hr["host_cpus"] > 0:
            # Docker --cpus is a quota (shares), not a reservation: exceeding
            # host CPUs degrades everyone under load, it doesn't hard-fail —
            # so this is a warning, never a refusal.
            over = hr["new_cpus"] - hr["host_cpus"]
            hr["cpu_overcommit"] = max(0, over)
            if over > 0:
                warns.append(
                    f"CPU limits would exceed host CPUs by {over:g} "
                    f"({hr['new_cpus']:g} CPU limits on {hr['host_cpus']} CPU host). "
                    "CPU limits are shares, not reservations — expect slower "
                    "instances under load, not hard failures.")
        bf = hr["binder_free_triplets"]
        if bf is not None and count > bf:
            cap = FLEET_BINDER_DEVICE_CAP // 3
            warns.append(
                f"only {bf} binder triplet(s) free now; the pool self-heals "
                f"up to ~{cap} instances per host.")
        if hr["image_present"] is False:
            if provider_id == "local":
                where = "this host"
            else:
                nm = load_providers().get(provider_id, {}).get("name", provider_id)
                where = "node %s" % nm
            warns.append(
                "the waydroid-instance image is not built on %s — "
                "create will fail until the image is built "
                "(pending-deploy/waydroid-image-build)." % where)
        ld = hr["loop_devices"]
        if ld is not None and ld < FLEET_LOOP_DEVICE_FLOOR:
            warns.append(
                f"only {ld} loop devices on this host (fleet floor "
                f"{FLEET_LOOP_DEVICE_FLOOR}) — instances past a few may fail "
                "to boot; the create path self-heals what it can, but fix "
                "the host config (install.sh step 4) or run "
                "pending-deploy/binder-verify/verify-binder.sh.")
        if hr["headroom_probe"] == "old-agent":
            warns.append(
                "node agent predates the headroom probe (1.0.21) — "
                "image/loop headroom unknown; re-run the node installer so "
                "the plan can verify the node before creating.")
        elif hr["headroom_probe"] == "unreachable":
            warns.append(
                "could not reach the node for headroom details — "
                "image/loop state unknown; create may fail partway.")
        hr["warning"] = " ".join(warns)
    except Exception:
        pass
    return hr


def fleet_plan(prefix, count, cpus, memory_mb, provider):
    """Dry-run: validate + names + headroom. No side effects.
    Returns (http_code, dict)."""
    clean, err = _fleet_validate(prefix, count, cpus, memory_mb, provider)
    if err:
        return err
    with _lock:
        instances = load_instances()
        names = [n for n in fleet_plan_names(instances, clean["prefix"], clean["count"])
                 if n not in instances]
        headroom = fleet_headroom(instances, clean["provider"], clean["count"],
                                  clean["memory_mb"], cpus=clean["cpus"])
    if not names:
        return 400, {"error": "all planned names already exist"}
    return 200, {"names": names, "total": len(names),
                 "prefix": clean["prefix"], "count": clean["count"],
                 "cpus": clean["cpus"], "memory_mb": clean["memory_mb"],
                 "provider": clean["provider"], "headroom": headroom}


def _start_fleet_create_job(names, prefix, cpus, memory_mb, provider):
    """Start a fleet-create worker job with explicit names.

    Returns (202, dict). The worker journals per-instance progress like
    every other batch job. Used by fleet_create_job (plan-generated names)
    and batch_resume_job (pending names of an interrupted job).
    """
    with _lock:
        if any(j.get("status") == "running" for j in _batch_jobs.values()):
            return 409, {"error": "a batch job is already running"}
    _sweep_batch_jobs()
    jid = secrets.token_hex(8)
    job = {"job_id": jid, "action": "create-fleet", "names": list(names),
           "prefix": prefix, "cpus": cpus, "memory_mb": memory_mb,
           "provider": provider, "total": len(names), "done": 0, "results": [],
           "status": "running", "created": time.time()}
    with _lock:
        _batch_jobs[jid] = job
        _journal_batch_job(job)
    threading.Thread(target=_run_fleet_create, args=(jid,), daemon=True,
                     name=f"fleet-create-{jid}").start()
    return 202, {"job_id": jid, "action": "create-fleet",
                 "total": len(names), "names": list(names),
                 "provider": provider}


def fleet_create_job(prefix, count, cpus, memory_mb, provider):
    """Validate and start a fleet-create job. Returns (http_code, dict)."""
    clean, err = _fleet_validate(prefix, count, cpus, memory_mb, provider)
    if err:
        return err
    with _lock:
        if any(j.get("status") == "running" for j in _batch_jobs.values()):
            return 409, {"error": "a batch job is already running"}
        instances = load_instances()
        names = [n for n in fleet_plan_names(instances, clean["prefix"], clean["count"])
                 if n not in instances]
    if not names:
        return 400, {"error": "all planned names already exist"}
    return _start_fleet_create_job(names, clean["prefix"], clean["cpus"],
                                   clean["memory_mb"], clean["provider"])


def _run_fleet_create(jid):
    # Last-resort thread net (same contract as _run_batch_job): an
    # exception escaping the per-instance try/except must mark the job
    # interrupted instead of stranding it "running" forever.
    try:
        with _lock:
            job = _batch_jobs.get(jid)
            names = list(job["names"]) if job else []
            cpus = job.get("cpus", 2) if job else 2
            memory_mb = job.get("memory_mb", 2048) if job else 2048
            provider = job.get("provider", "local") if job else "local"
        for name in names:
            try:
                if provider == "local":
                    code, resp = create_local_instance(name, cpus, memory_mb)
                else:
                    code, resp = create_remote_instance(name, cpus, memory_mb, provider)
                ok = code == 200
                if ok:
                    err = None
                elif isinstance(resp, dict):
                    err = resp.get("error") or f"create failed (HTTP {code})"
                else:
                    err = f"create failed (HTTP {code})"
            except Exception as e:  # never let one instance kill the batch
                ok, err = False, f"exception: {type(e).__name__}"
            with _lock:
                job = _batch_jobs.get(jid)
                if not job:
                    return
                job["results"].append({"name": name, "ok": ok, "error": err})
                job["done"] += 1
                _journal_batch_job(job)  # progress survives a manager restart
        with _lock:
            job = _batch_jobs.get(jid)
            if job:
                job["status"] = "done"
                _journal_batch_job(job)
    except BaseException as e:
        _mark_job_interrupted(jid, "worker error: " + type(e).__name__)


RESUMABLE_ACTIONS = BATCH_ACTIONS + ("create-fleet",)
_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _clean_resume_name(n):
    """Sanitize a journal-sourced instance name for resume.

    Journal files are trusted local state, but a hand-edited or corrupt
    journal must never turn into shell metacharacters inside the create
    worker (docker_inspect interpolates the name into shell=True).
    Returns the clean name, or None.
    """
    s = str(n or "")
    return s if _JOB_NAME_RE.match(s) else None


def batch_resume_job(jid):
    """Resume an interrupted batch job on its never-attempted instances.

    Creates a NEW job (fresh job_id) covering only the names the original
    job never attempted — failed-but-attempted instances are NOT retried
    (they're listed as failed on the dashboard). The interrupted job stays
    pollable as history until the normal TTL sweep. Returns (http_code,
    dict). Best-effort, never raises.
    """
    try:
        with _lock:
            job = _batch_jobs.get(jid)
            if not job:
                return 404, {"error": "job not found"}
            if job.get("status") != "interrupted":
                return 409, {"error": "only interrupted jobs can be resumed"}
            if any(j.get("status") == "running" for j in _batch_jobs.values()):
                return 409, {"error": "a batch job is already running"}
            action = job.get("action")
            if action not in RESUMABLE_ACTIONS:
                return 409, {"error": "this job type cannot be resumed"}
            attempted = {r.get("name") for r in job.get("results", [])
                         if isinstance(r, dict)}
            names = job.get("names")
            if not isinstance(names, list):
                return 409, {"error": "nothing left to resume"}
            pending = []
            for n in names:
                clean = _clean_resume_name(n)
                if clean and clean not in attempted:
                    pending.append(clean)
            orig = {"prefix": job.get("prefix"), "cpus": job.get("cpus", 2),
                    "memory_mb": job.get("memory_mb", 2048),
                    "provider": job.get("provider", "local")}
        if not pending:
            return 409, {"error": "nothing left to resume"}
        if action == "create-fleet":
            instances = load_instances()
            # Names created outside the job meanwhile would 409 in the
            # worker — skip them so the resume only attempts what's missing.
            pending = [n for n in pending if n not in instances]
            if not pending:
                return 409, {"error": "nothing left to resume"}
            code, resp = _start_fleet_create_job(
                pending, orig["prefix"], orig["cpus"],
                orig["memory_mb"], orig["provider"])
        else:
            instances = load_instances()
            # Instances deleted meanwhile can't be acted on — drop them
            # rather than failing the whole resume (batch_create_job 400s
            # on unknown names).
            pending = [n for n in pending if n in instances]
            if not pending:
                return 409, {"error": "nothing left to resume"}
            code, resp = batch_create_job(action, pending)
        if code == 202:
            with _lock:
                new_job = _batch_jobs.get(resp.get("job_id"))
                if new_job:
                    new_job["resumed_from"] = jid
                    _journal_batch_job(new_job)
        return code, resp
    except Exception:
        return 500, {"error": "resume failed"}

# ---------------------------------------------------------------- HTTP handler

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class Handler(BaseHTTPRequestHandler):
    server_version = "Jerusalem 1.0/1.0"

    def log_message(self, fmt, *args):
        pass  # quiet

    # ---- helpers ----
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _send_text(self, code, text, ctype="text/plain"):
        self._send(code, text, ctype)

    def _serve_file(self, fpath, ctype):
        try:
            with open(fpath, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self._json(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        # stream in chunks to avoid huge memory spike
        for i in range(0, len(data), 65536):
            self.wfile.write(data[i:i+65536])

    def _read_json(self, max_bytes=2097152):
        # Non-dict JSON (a list, string, number, true, null) is NOT a
        # request body: every caller does data.get(...), so a bare
        # `["x"]` body raised AttributeError, the handler thread died,
        # and the connection dropped with NO response — including on
        # the unauthenticated /api/login route. Coerce to {} here, the
        # single choke point, so all 12 callers get a clean 400/401
        # from their normal validation instead of a dropped connection.
        try:
            ln = int(self.headers.get("Content-Length", 0))
            if ln < 0 or ln > max_bytes:
                return {}
            parsed = json.loads(self.rfile.read(ln) or b"{}")
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}

    def _read_body(self, max_bytes=2097152):
        # Raw-body twin of _read_json, same 2MB cap. POST /resize and POST
        # /roblox-account called this method since the first commit, but
        # it never existed: both routes raised AttributeError and the
        # connection dropped with no response, so dashboard Resize and the
        # per-instance Account Save never worked (bulk import uses a
        # different endpoint, which is why nobody noticed). Callers do
        # json.loads(body or "{}"), so invalid JSON still surfaces as
        # their 400. An over-cap or garbage length returns b"" WITHOUT
        # reading the body — a declared 10GB body must never be paged into
        # RAM. Never raises.
        try:
            ln = int(self.headers.get("Content-Length", 0))
            if ln < 0 or ln > max_bytes:
                return b""
            return self.rfile.read(ln) if ln else b""
        except Exception:
            return b""

    def _authed(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        tok = auth[7:].strip()
        with _lock:
            exp = _tokens.get(tok)
            if not exp or exp < time.time():
                _tokens.pop(tok, None)
                return False
        return True

    # ---- routing ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # WebSocket screen stream
        if path.startswith("/ws/screen/"):
            self._handle_ws(path[len("/ws/screen/"):])
            return

        # H.264 video over WebSocket (fMP4 + MSE; works through the tunnel)
        if path.startswith("/ws/h264/"):
            self._handle_ws_h264(path[len("/ws/h264/"):])
            return

        # ---- agent self-update download (agent-key auth via headers) ----
        # Wing agents (1.0.24+) pull their own update from the manager they
        # heartbeat to, after the heartbeat response advertises a newer
        # version. Auth is the provider's agent key in headers (never the
        # query string); the agent verifies the sha256 from the heartbeat
        # directive against these bytes before swapping them in.
        if path == "/api/agent/update/download":
            pid = self.headers.get("X-Provider-Id", "")
            key = self.headers.get("X-Agent-Key", "")
            providers = load_providers()
            p = providers.get(pid) if isinstance(providers, dict) else None
            stored = p.get("agent_key") if isinstance(p, dict) else None
            # Fail closed on empty key either side: compare_digest("", "")
            # is True, so an empty stored key must never authenticate.
            if not stored or not key or not hmac.compare_digest(
                    str(key), str(stored)):
                return self._json(401, {"error": "unauthorized"})
            apath = _agent_disk_path()
            try:
                size = os.path.getsize(apath)
            except Exception:
                return self._json(500, {"error": "agent file unavailable"})
            if size > AGENT_UPDATE_MAX_BYTES:
                return self._json(500, {"error": "agent file too large"})
            try:
                with open(apath, "rb") as f:
                    data = f.read()
            except Exception:
                return self._json(500, {"error": "agent file unavailable"})
            if len(data) != size or len(data) > AGENT_UPDATE_MAX_BYTES:
                return self._json(500, {"error": "agent file changed"})
            return self._send(200, data, "text/x-python", extra={
                "X-Agent-Version": current_agent_version(),
                "X-Agent-Sha256": agent_file_sha256()})

        if path == "/" or path.startswith("/static/"):
            self._serve_static(path)
            return

        if path == "/api/host-stats":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, get_host_stats())

        # ---- watchdog event feed (dashboard "Eventos" modal) ----
        # Main-node log + every wing node's selfwatch log, merged
        # chronologically. Node events are best-effort: offline/old-agent
        # nodes simply contribute nothing.
        if path == "/api/watchdog-events":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            qs = dict(p.split("=", 1) for p in parsed.query.split("&")
                      if "=" in p)
            local = read_watchdog_events(qs.get("lines", "100"))
            if not local.get("ok"):
                return self._json(200, local)
            local_events = [{"line": e.get("line", ""),
                             "sev": e.get("sev", "info"), "node": None,
                             "ts": _event_ts(e.get("line"))}
                            for e in local.get("events", [])]
            node_events = fetch_node_watchdog_events(
                load_providers(), qs.get("lines", "100"))
            try:
                n = max(1, min(int(qs.get("lines", "100")),
                               WATCHDOG_EVENTS_MAX))
            except (TypeError, ValueError):
                n = 100
            merged = merge_watchdog_events(local_events, node_events)[-n:]
            nodes = sorted({e["node"] for e in merged if e.get("node")})
            return self._json(200, {"ok": True, "path": local["path"],
                                    "nodes": nodes, "events": merged})

        # ---- backup status (dashboard backup line) ----
        # backup-state.sh writes $DATA_DIR/last-backup.json on every run
        # (success and failure); the dashboard renders it in the fleet bar.
        if path == "/api/backup-status":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, read_backup_status())

        # ---- provider list (dashboard auth) ----
        if path == "/api/providers/static-token":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, {"join_token": get_static_token()})

        if path == "/api/providers/install-progress":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            with _lock:
                out = [{"install_id": k, **v} for k, v in INSTALL_PROGRESS.items()]
            out.sort(key=lambda x: x["updated_at"], reverse=True)
            return self._json(200, {"attempts": out})

        if path == "/api/providers":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            providers = load_providers()
            out = []
            latest_agent = current_agent_version()
            for pid, p in providers.items():
                # One corrupt record used to kill the whole list: a
                # non-dict record raised AttributeError on p.get, and a
                # string last_seen raised TypeError on the online
                # computation. A bad record is skipped; a dict record
                # with bad fields degrades (offline, empty stats) so
                # the rest of the list still renders.
                if not isinstance(p, dict):
                    continue
                # extract IP from agent URL
                ip = ""
                try:
                    ip = p.get("url", "").split("://")[1].split(":")[0].split("/")[0]
                except Exception:
                    pass
                ls = p.get("last_seen")
                sw = p.get("selfwatch")
                caps = p.get("host_caps")
                out.append({
                    "id": pid, "name": p.get("name"), "hostname": p.get("hostname"),
                    "os": p.get("os"), "url": p.get("url"), "ip": ip,
                    "created": p.get("created"),
                    "last_seen": (ls if isinstance(ls, (int, float))
                                  and not isinstance(ls, bool) else None),
                    "stats": provider_stats(p),
                    "online": time.time() - provider_last_seen(p) < 120,
                    "version": p.get("version", ""),
                    "agent_latest": latest_agent,
                    "agent_update": agent_update_available(p.get("version", ""), latest_agent),
                    "selfwatch": sw if isinstance(sw, dict) else {},
                    # Node host caps (agent >=1.0.27 reports them on every
                    # heartbeat; older agents / never-heartbeated -> None).
                    "agent_loop_devices": (caps if isinstance(caps, dict) else {}).get("loop_devices"),
                    "agent_image_present": (caps if isinstance(caps, dict) else {}).get("image_present"),
                })
            # include local as a pseudo-provider
            ls = get_host_stats()
            out.insert(0, {"id": "local", "name": "Main node (local)",
                           "hostname": "main", "os": "Linux", "url": "local",
                           "last_seen": time.time(), "stats": ls, "online": True,
                           "version": "local", "agent_latest": latest_agent,
                           "agent_update": None,
                           "agent_loop_devices": None, "agent_image_present": None})
            return self._json(200, {"providers": out})

        # ---- install files (join-token header auth, for wing installers) ----
        if path.startswith("/api/install/"):
            jt = self.headers.get("X-Join-Token", "")
            jt_info = JOIN_TOKENS.get(jt)
            # allow download if token valid (not yet used) OR static token OR dashboard-authed
            valid = ((jt_info and not jt_info.get("used")
                     and time.time() - jt_info["created"] < 3600)
                     or hmac.compare_digest(jt, get_static_token())
                     or self._authed())
            if not valid:
                return self._json(403, {"error": "invalid join token"})
            fname = path[len("/api/install/"):]
            # track downloads so the dashboard can show "downloaded but never started"
            if fname in ("install-wing.sh", "install-wing.bat"):
                with _lock:
                    dl_id = f"dl-{self.client_address[0]}-{int(time.time())}"
                    INSTALL_PROGRESS[dl_id] = {
                        "hostname": "", "os": "",
                        "stage": "downloaded",
                        "message": f"{fname} fetched, waiting for installer to start",
                        "ip": self.client_address[0],
                        "updated_at": time.time(),
                    }
            if fname == "agent.py":
                fp = os.path.join(WING_DIR, "agent.py")
                return self._serve_file(fp, "text/x-python")
            if fname in ("install-wing.sh", "install-wing.bat"):
                fp = os.path.join(WING_DIR, fname)
                # fill placeholders with this server's public URL + token
                # (agent name is generated client-side per install)
                try:
                    with open(fp) as f:
                        content = f.read()
                except FileNotFoundError:
                    return self._json(404, {"error": "not found"})
                host = self.headers.get("Host", "")
                proto = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
                base = f"{proto}://{host}"
                content = (content.replace("__MAIN_URL__", base)
                           .replace("__JOIN_TOKEN__", jt))
                if fname.endswith(".bat"):
                    # Windows batch files and the one-liner's findstr check
                    # need CRLF line endings; normalize on serve.
                    content = content.replace("\r\n", "\n").replace("\n", "\r\n")
                return self._send_text(200, content,
                                       "text/x-shellscript" if fname.endswith(".sh") else "text/plain")
            if fname == "wing-image.tar":
                if not os.path.exists(WING_IMAGE_TAR):
                    return self._json(404, {"error": "image not exported yet"})
                return self._serve_file(WING_IMAGE_TAR, "application/x-tar")
            if fname == "python-3.12.7-amd64.exe":
                fp = os.path.join(WING_DIR, "python-3.12.7-amd64.exe")
                if not os.path.exists(fp):
                    return self._json(404, {"error": "not found"})
                return self._serve_file(fp, "application/octet-stream")
            return self._json(404, {"error": "not found"})

        m = re.fullmatch(r"/api/instances/batch/([0-9a-f]+)", path)
        if m:
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            code, resp = batch_get_job(m.group(1))
            return self._json(code, resp)

        if path == "/api/instances":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            with _lock:
                instances = load_instances()
            providers = load_providers()
            # fetch remote statuses in one go per provider (fail-fast + a
            # short last-known-good cache so a wedged node can't stall the
            # 10s dashboard poll or flicker its cards to "stopped")
            remote_status = fetch_remote_statuses(providers)
            login_results = load_login_results()
            out = []
            for name, inst in instances.items():
                # One bad record used to kill the whole fleet poll: a
                # string record crashed on .get, and a string provider
                # record crashed on the pname lookup. Bad records are
                # skipped (like the t154 providers list); a bad
                # provider field reads as unusable, never as local.
                if not isinstance(inst, dict):
                    continue
                prov = instance_provider_of(inst)
                if prov is None:
                    continue
                prec = providers.get(prov)
                pname = "Main node" if prov == "local" else (
                    prec.get("name", prov) if isinstance(prec, dict) else prov)
                if prov != "local":
                    rs = remote_status.get((prov, name), {})
                    running = rs.get("running", False)
                    setup = rs.get("setup", "unknown")
                    status = "running" if running else "stopped"
                    lr = login_results.get(name)
                    lr = lr if isinstance(lr, dict) else None
                    out.append({
                        "name": name, "status": status, "setup": setup,
                        "provider": prov, "provider_name": pname,
                        # heartbeat staleness: without this, a dead node's
                        # cards read "stopped" exactly like a deliberately
                        # stopped instance (see node_offline() above)
                        "node_offline": node_offline(providers, prov),
                        # failing status endpoint on a heartbeating node:
                        # same "stopped" lie once the 30s cache expires,
                        # but node_offline is False (see
                        # remote_fetch_degraded() above)
                        "node_fetch_degraded": remote_fetch_degraded(
                            providers, prov),
                        "node_fetch_failures": remote_fetch_failures(prov),
                        "ip": inst.get("ip"), "cpus": inst.get("cpus"),
                        "memory_mb": inst.get("memory_mb"),
                        "instance_id": inst.get("instance_id"),
                        "created_at": inst.get("created_at"),
                        # selfwatch circuit-breaker state: agent >=1.0.22 reports
                        # degraded/_reason from its own per-instance markers;
                        # older agents omit the fields -> False/None (same as
                        # "no data")
                        "degraded": clean_agent_degraded(rs.get("degraded")),
                        "degraded_reason": clean_agent_degraded_reason(
                            rs.get("degraded_reason")),
                        # drift badge: agent >=1.0.5 reports roblox_drift/_reason
                        # from its own version-drift guard; older agents omit
                        # the fields -> False/None (same as "no data")
                        "roblox_drift": clean_agent_drift(rs.get("roblox_drift")),
                        "roblox_drift_reason": clean_agent_drift_reason(
                            rs.get("roblox_drift_reason")),
                        "login_ok": lr.get("ok") if lr else None,
                        "login_ts": lr.get("ts") if lr else None,
                        "login_by": lr.get("by") if lr else None,
                        # live usage: reported by the node agent (>=1.0.4);
                        # older agents omit the fields -> None (same as "no data")
                        "mem_used_mb": clean_agent_resource(rs.get("mem_used_mb")),
                        "cpu_pct": clean_agent_resource(rs.get("cpu_pct")),
                    })
                    continue
                # Per-instance docker snapshot (10s TTL): one docker inspect per
                # instance per 10s window instead of per poll.
                snap = instance_snapshot(name, inst)
                lr = login_results.get(name)
                lr = lr if isinstance(lr, dict) else None
                res = instance_resources(name)
                out.append({
                    "name": name,
                    "status": snap["status"],
                    "setup": snap["setup"],
                    "provider": "local",
                    "provider_name": "Main node",
                    # the manager answering the poll IS the local node
                    "node_offline": False,
                    "node_fetch_degraded": False,
                    "node_fetch_failures": 0,
                    "ip": snap["ip"],
                    "cpus": inst.get("cpus"),
                    "memory_mb": inst.get("memory_mb"),
                    "instance_id": inst.get("instance_id"),
                    "created_at": inst.get("created_at"),
                    "degraded": snap["degraded"],
                    "degraded_reason": snap["degraded_reason"],
                    "roblox_drift": snap["roblox_drift"],
                    "roblox_drift_reason": snap["roblox_drift_reason"],
                    "login_ok": lr.get("ok") if lr else None,
                    "login_ts": lr.get("ts") if lr else None,
                    "login_by": lr.get("by") if lr else None,
                    # live usage (None until the poller has data / stale entries)
                    "mem_used_mb": res["mem_used_mb"] if res else None,
                    "cpu_pct": res["cpu_pct"] if res else None,
                })
            # Fleet totals used to be local-node only while the bar is
            # labelled "RAM de flota" — wing-node usage was silently
            # missing. Remote entries in `out` already carry the agent's
            # cleaned per-instance usage; pass them (plus fresh wing-node
            # RAM) so the totals cover the whole fleet.
            remote_entries = [e for e in out if e.get("provider") != "local"]
            return self._json(200, {"instances": out,
                                    "fleet_resources": fleet_resource_summary(
                                        remote_instances=remote_entries,
                                        remote_host_ram_mb=remote_host_ram_mb(providers)),
                                    "watchdog_global": global_watchdog_health(),
                                    "nodes_attention": nodes_attention_summary(providers)})

        m = re.fullmatch(r"/api/instances/([^/]+)/roblox", path)
        if m:
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            return self._json(200, {"installed": check_roblox(name)})

        # Fleet-wide Roblox account coverage (usernames only, never passwords)
        if path == "/api/roblox-accounts":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, roblox_account_summary())

        # Get Roblox account info (username and auto_login flag, never password)
        m = re.fullmatch(r"/api/instances/([^/]+)/roblox-account", path)
        if m:
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            accounts = load_accounts()
            acct = account_of(accounts.get(name))
            return self._json(200, {
                "username": acct.get("username", ""),
                "has_password": bool(acct.get("password")),
                "auto_login": acct.get("auto_login", False),
            })

        # Last recorded Roblox login attempt for one instance
        # (outcome only, never credentials)
        m = re.fullmatch(r"/api/instances/([^/]+)/roblox-login-result", path)
        if m:
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            lr = load_login_results().get(name)
            return self._json(200, {"result": lr if isinstance(lr, dict) else None})

        self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/login":
            ip = _client_ip_from_headers(self.headers, self.client_address[0])
            if not login_allowed(ip):
                return self._json(429, {"error": "too many attempts"})
            data = self._read_json()
            if check_dashboard_password(data.get("password", "")):
                clear_login_failures(ip)
                tok = secrets.token_hex(24)
                with _lock:
                    _tokens[tok] = time.time() + 86400
                    save_tokens()
                return self._json(200, {"token": tok})
            record_login_failure(ip)
            return self._json(401, {"error": "bad password"})

        # ---- provider: wing registration (join-token auth, public) ----
        if path == "/api/providers/register":
            ip = _client_ip_from_headers(self.headers, self.client_address[0])
            now = time.time()
            with _lock:
                atts = [t for t in REGISTER_ATTEMPTS.get(ip, []) if now - t < 60]
                if len(atts) >= 10:
                    return self._json(429, {"error": "too many attempts"})
                atts.append(now)
                REGISTER_ATTEMPTS[ip] = atts
            data = self._read_json(max_bytes=4096)
            jt = str(data.get("join_token", ""))[:128]
            jt_info = JOIN_TOKENS.get(jt)
            is_static = hmac.compare_digest(jt, get_static_token())
            if is_static:
                pass
            elif not jt_info or jt_info.get("used") or time.time() - jt_info["created"] > 3600:
                return self._json(403, {"error": "invalid or expired join token"})
            else:
                jt_info["used"] = True
            pid = "p_" + secrets.token_hex(6)
            agent_key = gen_secret()
            providers = load_providers()
            # Stable machine identity: reuse existing provider if same machine re-registers
            machine_id = str(data.get("machine_id", ""))[:64]
            for existing_pid, existing in providers.items():
                if machine_id and existing.get("machine_id") == machine_id:
                    pid = existing_pid
                    agent_key = gen_secret()  # rotate key on re-register
                    break
            # Derive agent URL from the request source
            host = self.client_address[0]
            # Prefer explicit URL if wing sent one
            agent_url = data.get("agent_url") or f"http://{host}:8090"
            # A pre-existing corrupt record for this id (a string, not
            # a dict) used to raise AttributeError on .get("created")
            # and drop the public register request. A non-dict record
            # has no created time worth preserving.
            prev = providers.get(pid)
            created = (prev.get("created", time.time())
                       if isinstance(prev, dict) else time.time())
            providers[pid] = {
                "id": pid, "name": str(data.get("hostname", host))[:40],
                "hostname": str(data.get("hostname", ""))[:64],
                "os": str(data.get("os", ""))[:16],
                "machine_id": machine_id,
                "url": agent_url, "agent_key": agent_key,
                "created": created,
                "last_seen": time.time(), "stats": {},
            }
            save_providers(providers)
            return self._json(200, {"provider_id": pid, "agent_key": agent_key})

        # ---- provider: install progress reports (join-token auth, public) ----
        if path == "/api/providers/install-progress" and self.command == "POST":
            data = self._read_json(max_bytes=4096)
            jt = str(data.get("join_token", ""))[:128]
            jt_info = JOIN_TOKENS.get(jt)
            is_static = hmac.compare_digest(jt, get_static_token())
            if not is_static and (not jt_info or time.time() - jt_info["created"] > 3600):
                return self._json(403, {"error": "invalid join token"})
            iid = str(data.get("install_id", ""))[:32] or "unknown"
            with _lock:
                entry = INSTALL_PROGRESS.get(iid)
                if not entry or not isinstance(entry.get("log"), list):
                    entry = {
                        "hostname": str(data.get("hostname", ""))[:64],
                        "os": str(data.get("os", ""))[:16],
                        "ip": self.client_address[0],
                        "log": [],
                    }
                    INSTALL_PROGRESS[iid] = entry
                entry["log"].append({
                    "stage": str(data.get("stage", ""))[:32],
                    "message": str(data.get("message", ""))[:256],
                    "at": time.time(),
                })
                # keep last 30 lines per install
                entry["log"] = entry["log"][-30:]
                entry["stage"] = str(data.get("stage", ""))[:32]
                entry["message"] = str(data.get("message", ""))[:256]
                entry["updated_at"] = time.time()
                # keep only recent 50 installs
                if len(INSTALL_PROGRESS) > 50:
                    oldest = sorted(INSTALL_PROGRESS.items(), key=lambda x: x[1]["updated_at"])[:len(INSTALL_PROGRESS)-50]
                    for k, _ in oldest:
                        del INSTALL_PROGRESS[k]
            return self._json(200, {"ok": True})

        # ---- provider: heartbeat (agent-key auth) ----
        if path == "/api/providers/heartbeat":
            data = self._read_json()
            pid = data.get("provider_id", "")
            providers = load_providers()
            if not isinstance(providers, dict):
                # Corrupt providers file (e.g. a JSON list) used to crash
                # on providers.get below; treat as "no such provider".
                providers = {}
            if not isinstance(pid, str):
                # An unhashable provider_id (list/dict) used to raise
                # TypeError on providers.get, killing the handler thread
                # before the agent-key check ever ran.
                pid = ""
            p = providers.get(pid)
            if (not isinstance(p, dict) or not p.get("agent_key")
                    or not hmac.compare_digest(str(data.get("agent_key", "")),
                                               str(p.get("agent_key", "")))):
                # Non-dict record / missing key / empty stored key (an
                # empty stored key must never authenticate: compare_digest
                # ("", "") is True) all fail closed with a clean 403.
                return self._json(403, {"error": "bad credentials"})
            p["last_seen"] = time.time()
            stats = data.get("stats", {})
            if isinstance(stats, dict):
                clean = {}
                for k, v in list(stats.items())[:20]:
                    if isinstance(v, (int, float)) and abs(v) < 1e12:
                        clean[str(k)[:32]] = v
                p["stats"] = clean
            p["version"] = str(data.get("version", ""))[:16]
            sw = data.get("selfwatch", {})
            if isinstance(sw, dict):
                deg = sw.get("degraded", {})
                clean_deg = {}
                if isinstance(deg, dict):
                    for k, v in list(deg.items())[:20]:
                        nk = re.sub(r"[^A-Za-z0-9_-]", "", str(k))[:32]
                        if nk:
                            clean_deg[nk] = str(v)[:160]
                try:
                    rec = max(0, min(int(sw.get("recoveries", 0)), 10 ** 9))
                except (TypeError, ValueError, OverflowError):
                    # OverflowError: int(Infinity) in a heartbeat payload.
                    rec = 0
                p["selfwatch"] = {"recoveries": rec, "degraded": clean_deg,
                                  "updated": time.time()}
            # Agent 1.0.27+: host capability facts for the Nodes modal
            # (waydroid-instance image built? loop pool size?). Sanitized;
            # older agents omit the field -> stored all-None ("no data").
            try:
                caps = clean_agent_host_caps(data.get("host_caps"))
                caps["updated"] = time.time()
                p["host_caps"] = caps
            except Exception:
                pass
            inst = data.get("instances", [])
            if isinstance(inst, list):
                p["instances"] = [str(x)[:32] for x in inst[:50] if isinstance(x, str)]
            try:
                rport = int(data.get("port", 0))
                if rport and 1 <= rport <= 65535:
                    base = p.get("url", "")
                    p["url"] = re.sub(r":\d+$", f":{rport}", base) or base
            except (TypeError, ValueError, OverflowError):
                # OverflowError: int(Infinity) in a heartbeat payload.
                pass
            save_providers(providers)
            # Agent self-update (1.0.24+): tell an outdated agent a newer
            # wing/agent.py is available beside this manager. The agent
            # pulls it from /api/agent/update/download itself; outdated
            # agents that predate self-update simply ignore the field.
            resp = {"ok": True}
            try:
                directive = agent_update_directive(p.get("version", ""))
                if directive:
                    resp["agent_update"] = directive
            except Exception:
                pass
            return self._json(200, resp)

        if not self._authed():
            return self._json(401, {"error": "unauthorized"})

        # ---- provider management (dashboard auth) ----
        if path == "/api/providers/join-token":
            tok = gen_secret(24)
            JOIN_TOKENS[tok] = {"created": time.time(), "used": False}
            return self._json(200, {"join_token": tok, "expires_in": 3600})

        if path == "/api/providers/static-token/regenerate":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            try:
                os.remove(STATIC_TOKEN_FILE)
            except FileNotFoundError:
                pass
            return self._json(200, {"join_token": get_static_token()})

        if path == "/api/instances":
            return self._create_instance()

        if path == "/api/instances/batch":
            data = self._read_json()
            code, resp = batch_create_job(str(data.get("action", "")),
                                         data.get("names") or [])
            return self._json(code, resp)

        # Resume an interrupted batch job on its never-attempted instances
        # (202 + new job_id; the interrupted job stays pollable as history).
        m = re.fullmatch(r"/api/instances/batch/([0-9a-f]+)/resume", path)
        if m:
            code, resp = batch_resume_job(m.group(1))
            return self._json(code, resp)

        # Fleet bulk-create: dry-run plan (names + headroom, no side effects)
        if path == "/api/instances/fleet/plan":
            data = self._read_json()
            code, resp = fleet_plan(data.get("prefix"), data.get("count"),
                                   data.get("cpus", 2), data.get("memory_mb", 2048),
                                   data.get("provider", "auto"))
            return self._json(code, resp)

        # Fleet bulk-create: start the async create job (202 + job_id)
        if path == "/api/instances/fleet":
            data = self._read_json()
            code, resp = fleet_create_job(data.get("prefix"), data.get("count"),
                                         data.get("cpus", 2),
                                         data.get("memory_mb", 2048),
                                         data.get("provider", "auto"))
            return self._json(code, resp)

        # Bulk Roblox account import: {text: "name,username,password[,auto]\n..."}
        # or {accounts: [{name, username, password, auto_login}]}.
        if path == "/api/roblox-accounts/bulk":
            data = self._read_json()
            force_auto = bool(data.get("force_auto"))
            raw_accounts = data.get("accounts")
            if raw_accounts is not None:
                entries, line_errors = [], []
                if not isinstance(raw_accounts, list):
                    return self._json(400, {"error": "accounts must be a list"})
                for idx, item in enumerate(raw_accounts, 1):
                    if not isinstance(item, dict):
                        line_errors.append({"line": idx,
                                            "error": "entry must be an object"})
                        continue
                    name = str(item.get("name", "")).strip()
                    username = str(item.get("username", "")).strip()
                    password = str(item.get("password", ""))
                    auto = bool(item.get("auto_login"))
                    if not name:
                        line_errors.append({"line": idx,
                                            "error": "missing instance name"})
                        continue
                    if not username:
                        line_errors.append({"line": idx,
                                            "error": "missing username"})
                        continue
                    if not password:
                        line_errors.append({"line": idx,
                                            "error": "missing password"})
                        continue
                    entries.append({"name": name, "username": username[:100],
                                    "password": password[:200],
                                    "auto_login": auto})
            else:
                entries, line_errors = parse_bulk_accounts(data.get("text", ""))
            with _lock:
                saved, failed = bulk_save_accounts(entries, load_instances(),
                                                  force_auto=force_auto)
            return self._json(200, {"ok": True, "saved": saved,
                                   "failed": failed,
                                   "line_errors": line_errors})

        # Single-instance action (start/stop/restart share instance_single_action
        # with the batch worker, so the route must accept all three — restart
        # was missing here until single-restart-route, and POST .../restart
        # fell through to the 404 below even though the core supports it).
        m = re.fullmatch(r"/api/instances/([^/]+)/(start|stop|restart)", path)
        if m:
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            code, resp = instance_single_action(name, m.group(2))
            return self._json(code, resp)

        # Live RAM/CPU resize (docker update, no restart)
        m = re.fullmatch(r"/api/instances/([^/]+)/resize", path)
        if m:
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            try:
                body = json.loads(self._read_body() or "{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})
            clean, err = validate_resize_params(body)
            if err:
                return self._json(400, {"error": err})
            prov = instance_provider_of(load_instances().get(name))
            if prov is None:
                return self._json(404, {"error": "instance not found"})
            if prov == "local":
                code, resp = resize_local_instance(name, clean["memory_mb"],
                                                  clean["cpus"])
            else:
                code, resp = resize_remote_instance(name, clean["memory_mb"],
                                                   clean["cpus"], prov)
            return self._json(code, resp)

        m = re.fullmatch(r"/api/instances/([^/]+)/input", path)
        if m:
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            return self._handle_input(name)

        m = re.fullmatch(r"/api/instances/([^/]+)/webrtc/offer", path)
        if m:
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            return self._handle_webrtc_offer(name)

        m = re.fullmatch(r"/api/instances/([^/]+)/launch-roblox", path)
        if m:
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            code, resp = instance_single_action(name, "launch-roblox")
            return self._json(code, resp)

        # Roblox account credentials (per-instance auto-login) - POST only (GET is in do_GET)
        m = re.fullmatch(r"/api/instances/([^/]+)/roblox-account", path)
        if m:
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            try:
                body = json.loads(self._read_body() or "{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})
            if not isinstance(body, dict):
                # A string body containing "username" would pass the
                # `in` checks below and then crash on body["username"]
                # (TypeError, dropped connection); a list body would
                # silently no-op with a misleading 200. Clean 400.
                return self._json(400, {"error": "invalid json"})
            accounts = load_accounts()
            # account_of: a corrupt record is replaced by the new fields
            # instead of crashing on item assignment.
            acct = account_of(accounts.get(name))
            if "username" in body:
                acct["username"] = str(body["username"])[:100]
            if "password" in body and body["password"]:
                acct["password"] = str(body["password"])[:200]
            if "auto_login" in body:
                acct["auto_login"] = bool(body["auto_login"])
            if "clear" in body and body["clear"]:
                accounts.pop(name, None)
            else:
                accounts[name] = acct
            save_accounts(accounts)
            return self._json(200, {"ok": True})

        # Trigger Roblox auto-login now
        m = re.fullmatch(r"/api/instances/([^/]+)/roblox-login", path)
        if m and self.command == "POST":
            name = unquote(m.group(1))
            if not valid_instance_name(name):
                return self._json(404, {"error": "instance not found"})
            prov = instance_provider_of(load_instances().get(name))
            if prov is None:
                return self._json(404, {"error": "instance not found"})
            if prov == "local":
                cname = f"wd-{name}"
                if container_status(cname) != "running":
                    return self._json(409, {"error": "instance not running"})
            else:
                p = load_providers().get(prov)
                if not isinstance(p, dict) or not p:
                    return self._json(404, {"error": "node not found"})
                code, st = wing_request(p, "GET", f"/api/agent/instances/{name}/status",
                                       timeout=10)
                if not (200 <= code < 300) or not (isinstance(st, dict)
                                                  and st.get("running")):
                    return self._json(409, {"error": "instance not running"})
            ok, msg = roblox_login(name)
            return self._json(200 if ok else 400, {"ok": ok, "msg": msg})

        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
        if parsed.path == "/api/providers/install-progress":
            with _lock:
                INSTALL_PROGRESS.clear()
            return self._json(200, {"ok": True})
        m = re.fullmatch(r"/api/providers/([^/]+)", parsed.path)
        if m:
            pid = unquote(m.group(1))
            if pid == "local":
                return self._json(400, {"error": "cannot remove local"})
            providers = load_providers()
            # refuse if instances still assigned to it
            owned = [n for n, v in load_instances().items()
                     if instance_provider_of(v) == pid]
            if owned:
                return self._json(409, {"error": f"node still hosts: {', '.join(owned)}"})
            providers.pop(pid, None)
            save_providers(providers)
            return self._json(200, {"ok": True})
        m = re.fullmatch(r"/api/instances/batch/([0-9a-f]+)", parsed.path)
        if m:
            code, resp = batch_delete_job(m.group(1))
            return self._json(code, resp)

        m = re.fullmatch(r"/api/instances/([^/]+)", parsed.path)
        if not m:
            return self._json(404, {"error": "not found"})
        name = unquote(m.group(1))
        if not valid_instance_name(name):
            return self._json(404, {"error": "instance not found"})
        with _lock:
            instances = load_instances()
            prov = instance_provider_of(instances.get(name))
        if prov is None:
            return self._json(404, {"error": "instance not found"})
        # remote provider: proxy delete
        if prov != "local":
            providers = load_providers()
            p = providers.get(prov)
            if not isinstance(p, dict) or not p:
                return self._json(404, {"error": "node not found"})
            code, resp = wing_request(p, "DELETE", f"/api/agent/instances/{name}", timeout=60)
            if code == 200:
                with _lock:
                    instances = load_instances()
                    instances.pop(name, None)
                    save_instances(instances)
                # Prune the last-known-good snapshot so a stale entry can
                # never render a card for this instance again.
                drop_remote_status(prov, name)
                # Same stale-state sweep as the local path, minus the
                # watchdog state (that lives on the node — the agent's
                # delete sweeps its own selfwatch dir).
                drop_login_result(name)
                _stop_h264_broadcaster(name, prov)
            return self._json(code, resp if isinstance(resp, dict) else {"ok": code == 200})
        cname = f"wd-{name}"
        run(f"docker stop {cname}", timeout=60)
        run(f"docker rm -f {cname}", timeout=30)
        run(f"docker volume rm {cname}-varlib {cname}-data", timeout=30)
        with _lock:
            instances = load_instances()
            instances.pop(name, None)
            save_instances(instances)
        invalidate_instance_snapshot(name)
        # Delete-time stale-state sweep (delete-state-sweep): the
        # container and volumes are gone, but outcome/marker state keyed
        # by name survives in files the dashboard reads — a recreated
        # instance with the same name would inherit the dead instance's
        # login badge, DEGRADED badge, and version-drift badge until
        # the watchdog's next all-green cycle. Sweep them now. Saved
        # accounts (roblox_accounts.json) are deliberately NOT touched:
        # credentials are never deleted by this path.
        drop_login_result(name)
        drop_watchdog_state(name)
        drop_resource_cache(name)
        _stop_h264_broadcaster(name, "local")
        return self._json(200, {"ok": True})

    # ---- API implementations ----
    def _create_instance(self):
        data = self._read_json()
        name = re.sub(r"[^a-zA-Z0-9_-]", "", str(data.get("name", "")))[:32]
        if not name:
            return self._json(400, {"error": "name required (a-z, 0-9, -, _)"})
        try:
            cpus = float(data.get("cpus", 2))
            memory_mb = int(data.get("memory_mb", 4096))
        except (TypeError, ValueError, OverflowError):
            # OverflowError: int(Infinity) / float(huge int) from json.loads.
            return self._json(400, {"error": "invalid cpus/memory"})
        if not (0.5 <= cpus <= 32) or not (1024 <= memory_mb <= 32768):
            return self._json(400, {"error": "cpus 0.5-32, memory 1024-32768 MB"})

        providers = load_providers()
        want_prov = str(data.get("provider", "local"))
        if want_prov == "auto":
            want_prov = pick_provider(providers, need_ram_mb=memory_mb, need_cpus=cpus) or "local"
        if want_prov != "local" and want_prov not in providers:
            return self._json(400, {"error": "unknown node"})

        if want_prov != "local":
            code, resp = create_remote_instance(name, cpus, memory_mb, want_prov)
        else:
            code, resp = create_local_instance(name, cpus, memory_mb,
                                              ip=data.get("ip"))
        return self._json(code, resp)

    def _handle_input(self, name):
        data = self._read_json()
        prov = instance_provider_of(load_instances().get(name))
        if prov is None:
            return self._json(404, {"error": "instance not found"})
        ok, result = do_instance_input(name, prov, data)
        if not ok and result.get("error") == "node not found":
            return self._json(404, result)
        if not ok and result.get("error") == "instance not running":
            return self._json(409, result)
        if not ok:
            return self._json(400 if "required" in str(result.get("error", "")) or "unknown" in str(result.get("error", "")) else 500, result)
        return self._json(200, result)

    def _handle_webrtc_offer(self, name):
        if not HAS_AIORTC:
            return self._json(501, {"error": "webrtc not available"})
        data = self._read_json()
        offer_sdp = data.get("sdp", "")
        if not offer_sdp:
            return self._json(400, {"error": "sdp required"})
        prov = instance_provider_of(load_instances().get(name))
        if prov is None:
            return self._json(404, {"error": "instance not found"})
        if prov != "local":
            p = load_providers().get(prov)
            if not isinstance(p, dict) or not p:
                return self._json(404, {"error": "node not found"})
        answer_sdp = webrtc_offer_sync(name, prov, offer_sdp, webrtc_input_handler)
        if not answer_sdp:
            return self._json(500, {"error": "webrtc failed"})
        return self._json(200, {"sdp": answer_sdp, "type": "answer"})

    # ---- static ----
    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        fpath = os.path.normpath(os.path.join(STATIC_DIR, path[len("/static/"):] if path.startswith("/static/") else path.lstrip("/")))
        # Containment must be a whole-directory match: the old bare
        # startswith(STATIC_DIR) also accepted a SIBLING directory sharing
        # the prefix (e.g. static-evil), so an unauthenticated
        # GET /static/../static-evil/<file> served files outside the
        # static tree. A sibling file (../app.py, ../instances.json) was
        # never reachable — only a static*-prefixed sibling directory —
        # but the check was wrong regardless. The sep-suffixed prefix
        # (plus the exact-dir case) closes it.
        if not (fpath == STATIC_DIR
                or fpath.startswith(STATIC_DIR + os.sep)) \
                or not os.path.isfile(fpath):
            return self._json(404, {"error": "not found"})
        ctype = "text/html"
        if fpath.endswith(".js"):
            ctype = "application/javascript"
        elif fpath.endswith(".css"):
            ctype = "text/css"
        elif fpath.endswith(".png"):
            ctype = "image/png"
        with open(fpath, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---- websocket ----
    def _handle_ws(self, name):
        name = unquote(name)
        if not valid_instance_name(name):
            self._send(404, b"instance not found")
            return
        if not self._authed_ws():
            self._send(401, b"unauthorized")
            return
        prov = instance_provider_of(load_instances().get(name))
        if prov is None:
            self._send(404, b"instance not found")
            return
        wing = load_providers().get(prov) if prov != "local" else None
        if prov != "local" and (not isinstance(wing, dict) or not wing):
            self._send(404, b"node not found")
            return
        if prov == "local":
            cname = f"wd-{name}"
            if container_status(cname) != "running":
                self._send(409, b"instance not running")
                return
        # Upgrade
        try:
            self.request.sendall(ws_handshake(
                {k.lower(): v for k, v in self.headers.items()}))
        except OSError:
            return
        sock = self.request
        sock.settimeout(10)
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                op, _ = ws_read_frame(sock)
                if op == WS_READ_TIMEOUT:
                    continue  # idle wake-up, not a close: keep waiting
                if op is None or op == 0x8:  # close/error
                    stop.set()
                    break

        threading.Thread(target=reader, daemon=True).start()
        # Ensure the screencap script exists (one-time setup per container)
        if prov == "local":
            ensure_cap_script(name)
        # Adaptive screencap: this viewer keeps the in-container capture
        # loop fast until the socket closes (finally block below).
        stream_viewer_open(prov, name)

        last_frame = time.time()  # frameless-guard clock (see WS_FRAMELESS_TIMEOUT)
        last_sent = None   # last JPEG pushed (send-on-change, see below)
        last_ping = time.time()  # last keepalive ping (see WS_PING_SECS)

        def get_frame():
            if wing:
                # Shared remote-screenshot cache: one wing fetch per 100ms
                # per instance no matter how many viewers are attached.
                # attempts=1 fail-fast lives inside the helper; the 15s
                # frameless guard below plus the dashboard's 3s reconnect
                # own the retry policy at the loop level.
                return get_remote_screenshot(wing, prov, name)
            return capture_screenshot(name)

        try:
            while not stop.is_set():
                t0 = time.time()
                frame = get_frame()
                if frame:
                    last_frame = t0
                    if frame != last_sent:
                        # Send-on-change: the local read is already
                        # mtime-cached, but re-sending an identical JPEG at
                        # 20Hz burns bandwidth for nothing. A static screen
                        # (boot, loading, idle game) now costs ~zero bytes;
                        # the dashboard keeps showing its last image.
                        try:
                            ws_send_binary(sock, frame)
                        except OSError:
                            break
                        last_sent = frame
                    if t0 - last_ping >= WS_PING_SECS:
                        # Keepalive: tells the dashboard the stream is alive
                        # (its 3s stall badge stays honest) and keeps idle
                        # proxies/NATs from dropping the socket. Never sent
                        # while the source is dead — a real stall must show
                        # as "congelada".
                        try:
                            ws_send_text(sock, "ping")
                        except OSError:
                            break
                        last_ping = t0
                elif time.time() - last_frame > WS_FRAMELESS_TIMEOUT:
                    # Frameless guard: no frames for WS_FRAMELESS_TIMEOUT —
                    # close so a dead source stops being hammered; the
                    # dashboard reconnects MJPEG on its own.
                    print(f"[ws] mjpeg {name}: no frames for "
                          f"{WS_FRAMELESS_TIMEOUT}s, closing (client "
                          "reconnects)", flush=True)
                    break
                # Target ~20 fps (0.05s per frame); actual rate limited by screencap speed
                dt = time.time() - t0
                if dt < 0.05:
                    stop.wait(0.05 - dt)
        finally:
            stop.set()
            stream_viewer_close(prov, name)  # adaptive-screencap marker
            try:
                sock.close()
            except OSError:
                pass

    def _handle_ws_h264(self, name):
        """H.264 video over WebSocket (fMP4 init + fragments for MSE).

        Same auth and frame sources as _handle_ws (MJPEG). Viewers share
        one broadcaster (one ffmpeg encoder) per instance; the dashboard
        falls back to MJPEG if MSE is unsupported or this endpoint 501s
        (no ffmpeg/PIL).
        """
        name = unquote(name)
        if not valid_instance_name(name):
            self._send(404, b"instance not found")
            return
        if not HAS_H264:
            self._send(501, b"h264 unavailable (ffmpeg or PIL missing)")
            return
        if not self._authed_ws():
            self._send(401, b"unauthorized")
            return
        prov = instance_provider_of(load_instances().get(name))
        if prov is None:
            self._send(404, b"instance not found")
            return
        wing = load_providers().get(prov) if prov != "local" else None
        if prov != "local" and (not isinstance(wing, dict) or not wing):
            self._send(404, b"node not found")
            return
        if prov == "local":
            cname = f"wd-{name}"
            if container_status(cname) != "running":
                self._send(409, b"instance not running")
                return
        try:
            self.request.sendall(ws_handshake(
                {k.lower(): v for k, v in self.headers.items()}))
        except OSError:
            return
        sock = self.request
        sock.settimeout(10)
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                op, _ = ws_read_frame(sock)
                if op == WS_READ_TIMEOUT:
                    continue  # idle wake-up, not a close: keep waiting
                if op is None or op == 0x8:  # close/error
                    stop.set()
                    break

        threading.Thread(target=reader, daemon=True).start()

        bcast = _get_h264_broadcaster(name, prov, wing)
        if bcast is None:  # no frames — encoder never primed
            stop.set()
            try:
                sock.close()
            except OSError:
                pass
            return
        sub = bcast.subscribe()
        if sub is None:  # broadcaster died between lookup and subscribe
            stop.set()
            try:
                sock.close()
            except OSError:
                pass
            return
        # Adaptive screencap: this viewer keeps the in-container capture
        # loop fast until unsubscribe (finally block below).
        stream_viewer_open(prov, name)
        try:
            while not stop.is_set():
                # First chunk is the fMP4 init segment; later chunks are
                # moof-aligned fragments — appendable to MSE as-is.
                data = sub.read(timeout=1.0)
                if data is None:  # broadcaster stopped
                    break
                if data:
                    try:
                        ws_send_binary(sock, data)
                    except OSError:
                        break
        finally:
            stop.set()
            bcast.unsubscribe(sub)
            stream_viewer_close(prov, name)  # adaptive-screencap marker
            try:
                sock.close()
            except OSError:
                pass

    def _authed_ws(self):
        # Token via query ?token= or Authorization header
        parsed = urlparse(self.path)
        q = parsed.query
        m = re.search(r"(?:^|&)token=([^&]+)", q)
        if m:
            tok = unquote(m.group(1))
            with _lock:
                exp = _tokens.get(tok)
                if exp and exp > time.time():
                    return True
        return self._authed()


# ---- background-thread supervisor -------------------------------------------
# df-watchdog v5 restarts the whole manager service when it stops answering,
# but a single dead background thread (auto-login watcher, resource poller) in
# an otherwise healthy service was invisible and unrecoverable short of a full
# restart (which drops every viewer). The supervisor reaps and restarts dead
# registered threads every SUPERVISOR_INTERVAL seconds, with a circuit breaker
# (>3 restarts/hour -> give up + one ALERT, needs a human). Restarts and
# give-ups are appended to the watchdog log so the dashboard "Eventos" modal
# surfaces them. Everything here is best-effort and never raises.
_SUPERVISED = {}
_supervised_lock = threading.Lock()
SUPERVISOR_INTERVAL = 30
SUPERVISOR_MAX_RESTARTS = 3
SUPERVISOR_WINDOW = 3600


def _sup_log(msg):
    """Append one timestamped line to the watchdog log (Eventos modal reads
    it). Best-effort, never raises."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        with open(WATCHDOG_LOG, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def register_supervised(name, factory):
    """Register a background thread for supervision. factory() must return an
    unstarted threading.Thread. Best-effort, never raises."""
    try:
        with _supervised_lock:
            _SUPERVISED[name] = {"factory": factory, "thread": None,
                                 "restarts": [], "given_up": False}
    except Exception:
        pass


def _supervise_entry(name, entry):
    """One pass over one registered thread. Never raises."""
    if entry.get("given_up"):
        return
    t = entry.get("thread")
    try:
        alive = t is not None and t.is_alive()
    except Exception:
        alive = False
    if alive:
        return
    now = time.time()
    recent = [ts for ts in entry.get("restarts", []) if now - ts < SUPERVISOR_WINDOW]
    entry["restarts"] = recent
    if len(recent) >= SUPERVISOR_MAX_RESTARTS:
        entry["given_up"] = True
        _sup_log(f"ALERT supervisor gave up restarting '{name}' "
                 f"({SUPERVISOR_MAX_RESTARTS} restarts in the last hour) - "
                 f"needs human attention")
        return
    try:
        nt = entry["factory"]()
        nt.start()
    except Exception as ex:
        _sup_log(f"WARN supervisor failed to (re)start '{name}': {ex}")
        return
    entry["thread"] = nt
    recent.append(now)
    entry["restarts"] = recent
    _sup_log(f"WARN supervisor started background thread '{name}'")


def _supervise_once():
    """One supervisor pass over all registered threads. Never raises."""
    try:
        with _supervised_lock:
            items = list(_SUPERVISED.items())
    except Exception:
        return
    for name, entry in items:
        try:
            _supervise_entry(name, entry)
        except Exception:
            pass


def thread_supervisor():
    """Daemon loop: keep registered background threads alive. Never raises."""
    while True:
        try:
            _supervise_once()
        except Exception:
            pass
        time.sleep(SUPERVISOR_INTERVAL)


def main():
    load_tokens()
    _recover_batch_jobs()  # re-register journaled jobs; running -> interrupted
    # login-pwd-cleanup: a crash during a local login leaves a cleartext
    # pwd.txt in the instance volume; this startup sweep (before any
    # login thread starts) is the recovery path for exactly that crash.
    _swept = sweep_stale_pwd_files()
    if _swept:
        print(f"[login] swept {_swept} stale login staging file(s) left by an interrupted login")
    # state-tmp-sweep: same crash window, state dir instead of volumes —
    # a mid-write crash leaves FILE.tmp behind (accounts/tokens/token
    # tmps are credential-bearing) and nothing ever cleaned them.
    _swept_state = sweep_stale_state_tmp()
    if _swept_state:
        print(f"[state] swept {_swept_state} stale state tmp file(s) left by an interrupted write")
    if PASSWORD == "changeme":
        print("WARNING: using default password 'changeme'. Set JERUSALEM_PASSWORD!")
    if HAS_AIORTC:
        # The WebRTC event loop runs on its own thread; supervise it like the
        # other background threads so a dead loop is restarted (and surfaced
        # in the dashboard Eventos modal) instead of silently breaking every
        # WebRTC offer. The supervisor's first pass starts it immediately —
        # no direct .start() here, same pattern as the watcher/poller
        # registrations below. Until the first pass runs, _webrtc_loop is
        # None and offers fail fast to H.264/MJPEG.
        register_supervised("webrtc-loop", _webrtc_loop_thread_factory)
    register_supervised(
        "auto-login-watcher",
        lambda: threading.Thread(target=auto_login_watcher, daemon=True,
                                 name="auto-login-watcher"))
    register_supervised(
        "resource-poller",
        lambda: threading.Thread(target=resource_poller, daemon=True,
                                 name="resource-poller"))
    threading.Thread(target=thread_supervisor, daemon=True,
                     name="thread-supervisor").start()
    print(f"Jerusalem 1.0 manager on :{PORT} (PIL: {HAS_PIL}, WebRTC: {HAS_AIORTC}, H.264: {HAS_H264})")
    server = BoundedHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
