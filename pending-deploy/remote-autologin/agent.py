#!/usr/bin/env python3
import base64
import datetime
import glob
import hashlib
import hmac
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

VERSION = "1.0.45"
IS_WINDOWS = platform.system() == "Windows"

# --- instance-name gate for HTTP routes (agent 1.0.26; manager
# route-name-gate parity) ---
# The manager's route-name-gate (2026-10-19) fixed shell injection in its
# own routes and claimed "the wing agent needs nothing" because docker() is
# list-form and the marker readers fullmatch-reject. That claim missed two
# sinks that build FILESYSTEM paths from the raw URL segment:
# screenshot_bytes_linux() (/api/agent/instances/<n>/screenshot) and
# _find_fifo() via input_linux()/launch_roblox_linux()
# (/input, /launch-roblox). A name like
# a%2F..%2F..%2F..%2F..%2F..%2Fvar%2Flib%2Fdocker%2Fvolumes%2Fwd-rbx2
# (percent-encoded slashes sail through the ([^/]+) route regex, unquote
# runs after) resolved to ANOTHER instance's live.png / input.fifo:
# cross-instance screen read + cross-instance input injection for anyone
# holding the agent key hitting the agent directly (the manager path was
# already gated). Authenticated-only, no RCE (docker() is list-form, fifo
# writes are constrained `input ...` command strings) -- defense in depth
# and audit correctness. Same contract as the manager's
# valid_instance_name(): ^[A-Za-z0-9_-]{1,32}$, the exact create-time
# contract (create_instance_linux fullmatch-rejects). Route captures ->
# 404 "instance not found", indistinguishable from an unknown instance;
# the three filesystem sinks refuse invalid names outright.
def valid_agent_instance_name(name):
    """True for a well-formed instance name. Never raises."""
    try:
        return bool(isinstance(name, str) and
                    re.fullmatch(r"^[A-Za-z0-9_-]{1,32}$", name))
    except Exception:
        return False


# Agent-side parity with the manager's create-ip-gate: the agent's
# docker() is list-form (no shell), so a hostile ip could never inject
# a command here — but without a gate it reached `docker run --ip` and
# failed cryptically, and any future shell-form sink would inherit the
# hole. A strict dotted-quad is the only acceptable value: hostnames,
# IPv6, CIDR, and anything carrying shell metacharacters are rejected
# before docker is ever invoked. Never raises.
def valid_agent_ipv4(s):
    """True for a strict dotted-quad IPv4 literal. Never raises.

    Deliberately stricter than the manager's valid_ipv4 (which strips):
    the agent passes ip verbatim into the docker run argument list, so
    the validated value must be exactly the value docker receives —
    surrounding whitespace must fail here, not reach docker.
    """
    try:
        if not isinstance(s, str):
            return False
        if not s or any(c.isspace() for c in s):
            return False
        ipaddress.IPv4Address(s)
        return True
    except Exception:
        return False

# --- agent-key brute-force protection (manager login-ratelimit parity) ---
# The agent listens on 8090-8099, internet-facing on wing nodes. Before this
# item, failed X-Agent-Key attempts were unlimited and the key compare was a
# plain ==. Sliding window: AUTH_RATE_MAX failed attempts per
# AUTH_RATE_WINDOW seconds per client IP -> HTTP 429 {"error":
# "too many attempts"} (same body as the manager's /api/login 429); a
# successful authed request clears that IP's counter. /api/agent/health
# stays unauthenticated by design (the agent's own self-check + manager
# probes) and never counts. All helpers best-effort, never raise: a limiter
# bug can never lock out the manager (fail-open), while a key-compare error
# fails closed (denied).
AUTH_RATE_MAX = 10        # failed auth attempts per window per IP before 429
AUTH_RATE_WINDOW = 60     # seconds
AUTH_RATE_MAX_IPS = 1024  # hard cap on tracked IPs, stalest-first eviction
_AUTH_ATTEMPTS = {}       # ip -> [timestamps of failed agent-key auths]
_AUTH_LOCK = threading.Lock()


def _auth_key_ok(received):
    """Constant-time agent-key compare. Never raises; an empty/unset
    AGENT_KEY or non-string input refuses (fail closed)."""
    try:
        if not AGENT_KEY or not isinstance(received, str):
            return False
        return hmac.compare_digest(received, AGENT_KEY)
    except Exception:
        return False


def _prune_auth_attempts(now):
    """Drop expired failure timestamps; enforce the hard IP cap
    stalest-first. Best-effort, never raises. Eviction is fail-open for the
    evicted IP."""
    try:
        for ip in list(_AUTH_ATTEMPTS.keys()):
            recent = [t for t in _AUTH_ATTEMPTS[ip]
                      if now - t < AUTH_RATE_WINDOW]
            if recent:
                _AUTH_ATTEMPTS[ip] = recent
            else:
                _AUTH_ATTEMPTS.pop(ip, None)
        if len(_AUTH_ATTEMPTS) > AUTH_RATE_MAX_IPS:
            ordered = sorted(_AUTH_ATTEMPTS.items(),
                             key=lambda kv: kv[1][-1] if kv[1] else 0)
            for ip, _ in ordered[:len(ordered) - AUTH_RATE_MAX_IPS]:
                _AUTH_ATTEMPTS.pop(ip, None)
    except Exception:
        pass


def auth_allowed(ip):
    """Sliding-window rate limit on failed agent-key auths, per IP.
    Never raises; unknown IPs are always allowed."""
    now = time.time()
    try:
        with _AUTH_LOCK:
            atts = [t for t in _AUTH_ATTEMPTS.get(ip, [])
                    if now - t < AUTH_RATE_WINDOW]
            if atts or ip in _AUTH_ATTEMPTS:
                _AUTH_ATTEMPTS[ip] = atts
            if len(_AUTH_ATTEMPTS) > AUTH_RATE_MAX_IPS:
                _prune_auth_attempts(now)
            return len(atts) < AUTH_RATE_MAX
    except Exception:
        return True


def record_auth_failure(ip):
    """Count one failed agent-key auth for ip. Never raises."""
    try:
        with _AUTH_LOCK:
            atts = _AUTH_ATTEMPTS.get(ip, [])
            atts.append(time.time())
            _AUTH_ATTEMPTS[ip] = atts
    except Exception:
        pass


def clear_auth_failures(ip):
    """A successful authed request resets the IP's failure counter.
    Never raises."""
    try:
        with _AUTH_LOCK:
            _AUTH_ATTEMPTS.pop(ip, None)
    except Exception:
        pass


def _agent_client_ip(headers, peer):
    """Limiter key for the agent-key rate limiter, proxy-aware. Never raises.

    Parity with the manager's _client_ip_from_headers (login-ratelimit-tunnel,
    2026-10-20): a wing node can sit behind a reverse proxy or tunnel too, in
    which case every proxied agent user shares the same TCP peer (loopback)
    — keying on the peer alone makes one user's 10 bad attempts 429
    everyone, including the manager's own keyed requests (screenshots,
    status, self-update) from the same host. When the peer is a loopback
    address, prefer the client IP the proxy reports: CF-Connecting-IP first,
    then the leftmost X-Forwarded-For entry. The candidate must parse as an
    IP literal or the peer is kept (fail closed to the old behavior).
    Headers are NEVER trusted for non-loopback peers (spoof-proof)."""
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

# --- HTTP server bounds: slowloris / thread-starvation hardening ---
# Stock ThreadingHTTPServer spawns one thread per connection with no cap and
# no per-connection timeout: half-open or dribble-feed clients pin handler
# threads forever and starve the agent (wing nodes are internet-facing too).
# BoundedHTTPServer caps concurrent handler threads (excess connections fail
# fast with 503 instead of piling up threads) and drops connections idle
# longer than the socket timeout. Mirrors manager/app.py (the agent is a
# single deployed file and cannot import from the manager).
HTTP_MAX_THREADS = 64    # concurrent handler threads; legit peak is tiny
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

if IS_WINDOWS:
    _wname = "__WING_NAME__"
    if _wname.startswith("__"):
        # placeholder not replaced; derive from script location
        _wname = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), _wname)
else:
    _wname = "__WING_NAME__"
    if _wname.startswith("__"):
        _wname = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join("/root", _wname)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
KEY_PATH = os.path.join(DATA_DIR, ".agent_key")
INSTANCES_PATH = os.path.join(DATA_DIR, "instances.json")

IMAGE = "waydroid-instance:latest"
NETWORK = "__WING_NAME__"
SUBNET_BASE = 240
DISP_W, DISP_H = 1024, 608


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def load_key():
    try:
        with open(KEY_PATH) as f:
            return f.read().strip()
    except Exception:
        return ""


CONFIG = load_config()
AGENT_KEY = load_key()
PORT = int(CONFIG.get("port", 8090))
MAIN_URL = CONFIG.get("main_url", "").rstrip("/")
PROVIDER_ID = CONFIG.get("provider_id", "")


def run(cmd, timeout=30, shell=False):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=shell)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:
        return 1, str(e)


def docker(*args, timeout=60):
    return run(["docker"] + list(args), timeout=timeout)


def load_instances():
    try:
        with open(INSTANCES_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_instances(data):
    tmp = INSTANCES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    # 600 BEFORE the replace (state-600-complete): the agent's
    # instances.json mirrored the manager's unhardened save — under
    # umask 022 it sat world-readable, exposing the node's instance
    # names and internal IPs. Same idiom as the manager saves; the
    # OSError guard keeps Windows agents (where chmod is a no-op for
    # these bits) saving normally. No version bump: no manager gate
    # depends on this save and self-update is sha-driven.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, INSTANCES_PATH)


def get_host_stats():
    cpu_count = os.cpu_count() or 1
    if IS_WINDOWS:
        cpu_pct, ram_pct, used_mb, total_mb = 0.0, 0.0, 0, 0
        try:
            rc, out = run(["wmic", "OS", "get", "FreePhysicalMemory,TotalVisibleMemorySize", "/value"], timeout=10)
            vals = dict(re.findall(r"(\w+)=(\d+)", out))
            total_kb = int(vals.get("TotalVisibleMemorySize", 0))
            free_kb = int(vals.get("FreePhysicalMemory", 0))
            total_mb = total_kb // 1024
            used_mb = (total_kb - free_kb) // 1024
            ram_pct = round(100.0 * used_mb / total_mb, 1) if total_mb else 0.0
        except Exception:
            pass
        try:
            rc, out = run(["wmic", "cpu", "get", "loadpercentage", "/value"], timeout=10)
            m = re.search(r"LoadPercentage=(\d+)", out)
            cpu_pct = float(m.group(1)) if m else 0.0
        except Exception:
            pass
        if total_mb == 0 or cpu_pct == 0.0:
            try:
                ps = ("$os=Get-CimInstance Win32_OperatingSystem;"
                      "$cpu=(Get-CimInstance Win32_Processor | "
                      "Measure-Object -Property LoadPercentage -Average).Average;"
                      "\"{0} {1} {2}\" -f $os.TotalVisibleMemorySize,"
                      "$os.FreePhysicalMemory,[int]$cpu")
                rc, out = run(["powershell", "-NoProfile", "-Command", ps], timeout=15)
                parts = out.strip().split()
                if len(parts) >= 3:
                    total_kb, free_kb = int(parts[0]), int(parts[1])
                    total_mb = total_kb // 1024
                    used_mb = (total_kb - free_kb) // 1024
                    ram_pct = round(100.0 * used_mb / total_mb, 1) if total_mb else 0.0
                    cpu_pct = float(parts[2])
            except Exception:
                pass
        return {"cpu_percent": cpu_pct, "ram_percent": ram_pct,
                "ram_used_mb": used_mb, "ram_total_mb": total_mb, "cpu_count": cpu_count}
    cpu_pct = 0.0
    try:
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
        idle, total = vals[3] + vals[4], sum(vals)
        time.sleep(0.5)
        with open("/proc/stat") as f:
            vals2 = list(map(int, f.readline().split()[1:]))
        idle2, total2 = vals2[3] + vals2[4], sum(vals2)
        d_idle, d_total = idle2 - idle, total2 - total
        if d_total:
            cpu_pct = round(100.0 * (1 - d_idle / d_total), 1)
    except Exception:
        pass
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
            "ram_used_mb": used_mb, "ram_total_mb": total_mb, "cpu_count": cpu_count}


def require_linux():
    if IS_WINDOWS:
        return False
    return True


# ---- per-instance live resources (mirrors the manager's docker-stats parse) ----
_RES_SIZE_MULT = {"B": 1 / 1048576, "KB": 1 / 1024, "MB": 1, "GB": 1024,
                  "TB": 1048576, "KIB": 1 / 1024, "MIB": 1, "GIB": 1024,
                  "TIB": 1048576}
_INST_RES_INTERVAL = 15   # one docker stats call serves this window
_INST_RES_TTL = 60        # data older than this is "no data", not stale lies
_inst_res = {"ts": 0.0, "ok_ts": 0.0, "data": {}}
_inst_res_lock = threading.Lock()


def parse_size_mb(s):
    """'412.5MiB'/'1.2GiB'/'512KiB'/'100B' -> float MB, None on garbage."""
    try:
        m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", str(s))
        if not m:
            return None
        mult = _RES_SIZE_MULT.get(m.group(2).upper())
        if mult is None:
            return None
        return float(m.group(1)) * mult
    except (ValueError, TypeError, AttributeError):
        return None


def parse_docker_stats(output):
    """Parse `docker stats --no-stream --format '{{.Name}}\\t{{.MemUsage}}\\t{{.CPUPerc}}'.

    Returns {instance_name: {"mem_used_mb": f, "cpu_pct": f}} for wd-* containers.
    Skips malformed lines, non-wd names, and unsafe names.
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


def instance_resources_all():
    """Cached per-instance {mem_used_mb, cpu_pct} for wd-* containers.

    One docker-stats call per 15s window; data older than 60s is dropped.
    Linux only; best-effort, never raises.
    """
    now = time.time()
    with _inst_res_lock:
        fresh = now - _inst_res["ok_ts"] <= _INST_RES_TTL
        cached = dict(_inst_res["data"]) if fresh else {}
        if now - _inst_res["ts"] < _INST_RES_INTERVAL:
            return cached
        _inst_res["ts"] = now
    data = {}
    ok = False
    try:
        rc, out = docker("stats", "--no-stream", "--format",
                         "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}",
                         timeout=30)
        if rc == 0:
            data = parse_docker_stats(out)
            ok = True
    except Exception:
        pass
    with _inst_res_lock:
        if ok:
            _inst_res["data"] = data
            _inst_res["ok_ts"] = now
            return dict(data)
        # keep serving the last good data within TTL; otherwise "no data"
        if now - _inst_res["ok_ts"] <= _INST_RES_TTL:
            return dict(_inst_res["data"])
        return {}


def container_status(cname):
    rc, out = docker("ps", "-a", "--format", "{{.Names}}\t{{.Status}}")
    if rc != 0:
        return "unknown"
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] == cname:
            return "running" if parts[1].startswith("Up") else "stopped"
    return "missing"


def next_instance_id(used_ids):
    i = 1
    while i in used_ids:
        i += 1
    return i


WING_IMAGE_MISSING_MSG = (
    "waydroid-instance image not built on this node — "
    "build the Waydroid image on the node first "
    "(pending-deploy/waydroid-image-build), then create the instance"
)


def sw_image_present():
    """Is the instance image available to this node's docker? True/False/None.

    None = could not determine (docker daemon down, unexpected output):
    fail-open so the real `docker run` error surfaces instead of a false
    refusal. Only a positive "No such image" answer refuses the create.
    Best-effort: never raises.
    """
    try:
        rc, out = docker("image", "inspect", IMAGE, timeout=15)
        if rc == 0:
            return True
        if "no such image" in (out or "").lower():
            return False
        return None
    except Exception:
        return None


# ---- node host caps for the dashboard (agent 1.0.27) -------------------------
# The Create-fleet plan preview (1.0.21 /api/agent/headroom) could warn that
# the image was never built or the loop pool is the stock 8, but nobody
# watching the Nodes modal could see it — the 1.0.21 probe only runs at plan
# time. The heartbeat now carries host_caps so the Nodes modal shows both
# facts continuously (the vultr/node-capacity decision Penta keeps facing).
def sw_host_caps():
    """{'loop_devices': int|None, 'image_present': True|False|None}.

    Linux only — Windows nodes report None/None (honest "no data", same as
    the older-agent contract). Never raises.
    """
    try:
        if IS_WINDOWS:
            return {"loop_devices": None, "image_present": None}
        return {"loop_devices": sw_count_loop_devices(),
                "image_present": sw_image_present()}
    except Exception:
        return {"loop_devices": None, "image_present": None}


def sw_find_binder_triplet():
    """Locate a complete binder device triplet for a new container.

    binderfs may use the 'binder' or 'binder0' naming scheme; the triplet
    is [binder, vndbinder, hwbinder] as the create path bind-mounts them.
    Returns the list of 3 host device paths, or None when the pool does
    not hold a complete triplet. Never raises.
    """
    try:
        base = None
        for cand in ["/dev/binderfs/binder", "/dev/binderfs/binder0"]:
            if os.path.exists(cand):
                base = cand
                break
        if base is None:
            return None
        # determine the numbering scheme
        if base.endswith("binder0"):
            devs = ["/dev/binderfs/binder0", "/dev/binderfs/binder1",
                    "/dev/binderfs/binder2"]
        else:
            devs = ["/dev/binderfs/binder", "/dev/binderfs/binder1",
                    "/dev/binderfs/binder2"]
        if all(os.path.exists(d) for d in devs):
            return devs
        return None
    except Exception:
        return None


# Binder pool count ceiling for the two binder-pool sinks below
# (sw_ensure_binder_devices, sw_boot_binder_pool) and the expected-set
# build in _sw_prepare_pools_for_start: the fleet needs 3 devices per
# instance triplet and the default pool is 64, so 256 leaves 4x headroom
# while bounding the binder-control write loop. The env read for
# SW_BINDER_COUNT (DF_BINDER_COUNT) floors but never ceilings, so an
# absurd env value would otherwise loop that many shell commands per
# selfwatch pass / create attempt.
SW_BINDER_COUNT_MAX = 256


def _sw_coerce_binder_count(count):
    """Coerce a binder pool count to int in [1, SW_BINDER_COUNT_MAX], else
    None. Bools rejected, fractional floats rejected, junk strings
    rejected, NaN/Infinity fail the bounds check (int() raises first).
    Never raises."""
    try:
        if isinstance(count, bool):
            return None
        if isinstance(count, float):
            if not count.is_integer():
                return None
            count = int(count)
        elif isinstance(count, str):
            s = count.strip()
            if not s.isdigit():
                return None
            count = int(s)
        else:
            count = int(count)
    except (TypeError, ValueError, OverflowError):
        return None
    if 1 <= count <= SW_BINDER_COUNT_MAX:
        return count
    return None


def _sw_coerce_probe_port(port):
    """Coerce a probe port to int in [1, 65535], else None. Same
    coercion shape as _sw_coerce_binder_count: bools rejected,
    fractional floats rejected, junk strings rejected, out-of-range
    rejected. Never raises. (agent-sw-numerics-sink-gate: the old
    sw_probe_agent_http did int(port) with no range check — a bad port
    only failed the probe, but the sink itself validated nothing.)"""
    try:
        if isinstance(port, bool):
            return None
        if isinstance(port, float):
            if not port.is_integer():
                return None
            port = int(port)
        elif isinstance(port, str):
            s = port.strip()
            if not s.isdigit():
                return None
            port = int(s)
        else:
            port = int(port)
    except (TypeError, ValueError, OverflowError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _sw_coerce_probe_timeout(timeout):
    """Coerce a probe timeout to a positive finite float, else None.
    Bools, strings, None, zero/negative, NaN/Infinity all rejected.
    Never raises. (agent-sw-numerics-sink-gate: the old probe passed
    timeout straight to urlopen — a bad value only failed the probe,
    but the sink itself validated nothing.)"""
    try:
        if isinstance(timeout, bool):
            return None
        if isinstance(timeout, (int, float)):
            timeout = float(timeout)
        else:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if timeout > 0 and timeout == timeout and timeout != float("inf"):
        return timeout
    return None


def _sw_coerce_trim_bytes(value):
    """Coerce a log-trim byte count to a positive int, else None.
    Bools rejected, fractional floats rejected, junk strings rejected,
    zero/negative rejected. No upper bound: docker json logs legitimately
    run to tens of GB and the defaults are 1GB. Never raises.
    (agent-sw-numerics-sink-gate: the old sw_trim_container_logs used
    trim_max/trim_keep raw — a negative trim_keep made seek() land past
    EOF so the trim rewrote the log to ZERO bytes, and a negative
    trim_max made every log "over threshold" every pass.)"""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, float):
            if not value.is_integer():
                return None
            value = int(value)
        elif isinstance(value, str):
            s = value.strip()
            if not s.isdigit():
                return None
            value = int(s)
        else:
            value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if value >= 1:
        return value
    return None


def sw_ensure_binder_devices(count=64):
    """Recreate the binderfs device pool on demand (agent 1.0.16).

    Main-node parity with the manager's ensure_binder_devices() (2026-10-07
    binderfs-selfheal): a host reboot drops the binderfs pool back to the
    stock set, so instance create on a wing node failed with "binder devices
    not found" until a human recreated the pool by hand. Now the agent
    recreates the pool on the create path and retries once before failing.

    Creates binder, binder1..binderN via binder-control (names generated
    internally, no user input interpolated). On a brand-new host the
    binder_linux kernel module may never have been loaded (no binder
    devices at all) — a best-effort `modprobe binder_linux` is attempted
    when binder-control is missing (a no-op when the module is already
    loaded; a failure just falls through with the same outcome as before).
    NOTE: do NOT pass devices=<count> to modprobe. The binder 'devices'
    module parameter is a comma-separated NAME list (kernel
    CONFIG_ANDROID_BINDER_DEVICES semantics), not a count — a numeric value
    creates a single device literally named "64" and zero /dev/binder*
    nodes (this exact mistake was observed on a wing node).

    Linux only (the agent also runs on Windows; there the pool concept does
    not apply and this returns False). Best-effort, never raises. Returns
    True if at least one device was created.
    """
    if IS_WINDOWS:
        return False
    # agent-binder-count-sink-gate: coerce + bound the pool count at the
    # sink (was int() + floor-only — a huge count looped that many
    # binder-control shell commands, and DF_BINDER_COUNT has no ceiling
    # at the env read). Fails closed before any shell touch.
    count = _sw_coerce_binder_count(count)
    if count is None:
        return False
    try:
        rc, _ = run("test -e /dev/binderfs/binder-control", timeout=10, shell=True)
        if rc != 0:
            run("modprobe binder_linux 2>/dev/null", timeout=15, shell=True)
            rc, _ = run("test -e /dev/binderfs/binder-control", timeout=10, shell=True)
        if rc != 0:
            run("mkdir -p /dev/binderfs && mount -t binder binder /dev/binderfs 2>/dev/null",
                timeout=15, shell=True)
            rc, _ = run("test -e /dev/binderfs/binder-control", timeout=10, shell=True)
            if rc != 0:
                return False
        created = False
        for i in range(count):
            name = "binder" if i == 0 else "binder%d" % i
            rc, _ = run("test -e /dev/binderfs/%s" % name, timeout=10, shell=True)
            if rc != 0:
                rc2, _ = run("echo %s > /dev/binderfs/binder-control 2>/dev/null" % name,
                             timeout=10, shell=True)
                if rc2 == 0:
                    created = True
        return created
    except Exception:
        return False


def create_instance_linux(name, cpus, memory_mb, ip):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        return False, "invalid name"
    # agent-create-numerics-sink-gate: coerce + bound cpus/memory_mb at the
    # sink (the create route clamps today, but the sink itself took both
    # verbatim into the docker run argv and the state record — a future
    # internal caller passing an uncoercible, negative, or absurd value
    # would drive docker/state before any route gate ever ran). The sink
    # envelope matches the route's clamp output exactly (cpus 1-16,
    # memory 512-32768 MB), so every live caller passes unchanged; the
    # tighter manager-side create policy (0.5-32 / 1024+) stays enforced
    # upstream. Integral cpus normalize back to int so the valid-path
    # docker argv and state record are byte-identical to today. NaN fails
    # the bounds comparison; Infinity fails it too (int(Inf) raises first
    # and is caught). Fails closed before any docker/state touch.
    try:
        if isinstance(cpus, bool) or isinstance(memory_mb, bool):
            raise ValueError("bool numerics")
        cpus = float(cpus)
        if isinstance(memory_mb, float) and not memory_mb.is_integer():
            raise ValueError("fractional memory")
        memory_mb = int(memory_mb)
    except (TypeError, ValueError, OverflowError):
        return False, "invalid cpus/memory"
    if cpus.is_integer():
        cpus = int(cpus)
    if not (1 <= cpus <= 16) or not (512 <= memory_mb <= 32768):
        return False, "invalid cpus/memory"
    if not valid_agent_ipv4(ip):
        return False, "invalid ip"
    cname = f"wd-{name}"
    st = container_status(cname)
    if st == "unknown":
        return False, "docker unavailable"
    if st != "missing":
        return False, "already exists"
    # image preflight (agent 1.0.18 — manager waydroid_image_present parity):
    # fail fast with an actionable error when the instance image was never
    # built on this node, instead of a cryptic `docker run` failure deep in
    # the create. None (unknown) fails open so the real error surfaces.
    if sw_image_present() is False:
        return False, WING_IMAGE_MISSING_MSG
    inst = load_instances()
    used_ids = {v.get("instance_id", 0) for v in inst.values()}
    iid = next_instance_id(used_ids)
    # ensure the docker network exists (reuse existing 10.20.0.0/16 network if present)
    rc, out = docker("network", "ls", "--format", "{{.Name}}", timeout=10)
    net_name = NETWORK
    if NETWORK not in out.split():
        # check if 10.20.0.0/16 subnet is already used by another network
        rc2, out2 = docker("network", "ls", "--format", "{{.Name}}", timeout=10)
        found = None
        for n in out2.split():
            rc3, insp = docker("network", "inspect", n, "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", timeout=10)
            if "10.20.0.0/16" in insp:
                found = n
                break
        if found:
            net_name = found
        else:
            rc, err = docker("network", "create", "--subnet=10.20.0.0/16", NETWORK, timeout=20)
            if rc != 0:
                return False, f"failed to create network: {err.strip()}"
    # loop pool self-heal (agent 1.0.12): fail OPEN — loop nodes are consumed
    # at container boot, not reserved at create, so a short pool only warns;
    # the per-minute selfwatch guard repairs it. Never blocks creation.
    try:
        if not sw_ensure_loop_devices():
            _sw_log(SELF_WATCH_DIR,
                    "WARN loop device pool below floor at create time - instance created anyway")
    except Exception:
        pass
    # find binder devices (binderfs may have 'binder' or 'binder0' naming)
    binder_devs = sw_find_binder_triplet()
    if binder_devs is None:
        # binderfs self-heal (agent 1.0.16 — manager ensure_binder_devices
        # parity): a host reboot drops the pool back to the stock set, which
        # fails farm-wide instance creation on this node until a human
        # recreates the pool. Recreate via binder-control and retry once
        # before failing.
        try:
            sw_ensure_binder_devices()
        except Exception:
            pass
        binder_devs = sw_find_binder_triplet()
    if binder_devs is None:
        return False, "binder devices not found"
    cmd = [
        "run", "-d", "--name", cname, "--privileged",
        "--cgroupns=private",
        "-v", f"{binder_devs[0]}:/dev/binder",
        "-v", f"{binder_devs[1]}:/dev/vndbinder",
        "-v", f"{binder_devs[2]}:/dev/hwbinder",
        "-v", f"{cname}-varlib:/var/lib/waydroid",
        "-v", f"{cname}-data:/root/.local/share/waydroid",
        "--network", net_name, "--ip", ip,
        "--cpus", str(cpus), "--memory", f"{memory_mb}m",
        "-e", f"INSTANCE_ID={iid}",
        IMAGE,
    ]
    rc, out = docker(*cmd, timeout=120)
    if rc != 0:
        return False, f"docker run failed: {out[-300:]}"
    inst[name] = {"instance_id": iid, "cpus": cpus, "memory_mb": memory_mb,
                  "ip": ip, "created": time.time()}
    try:
        save_instances(inst)
    except Exception as e:
        docker("rm", "-f", cname, timeout=60)
        docker("volume", "rm", f"{cname}-data", timeout=30)
        docker("volume", "rm", f"{cname}-varlib", timeout=30)
        return False, f"failed to save state: {e}"
    return True, "created"


def delete_instance_linux(name):
    # agent-status-delete-sink-gate: refuse invalid names outright at the
    # sink (the DELETE route already 404s them, but the sink itself must
    # not drive docker/volume/state calls from a corrupted key — a future
    # internal caller must fail closed here, not at the route). False =
    # refused; True = deleted (the old code always returned True).
    if not valid_agent_instance_name(name):
        return False
    cname = f"wd-{name}"
    docker("rm", "-f", cname, timeout=60)
    docker("volume", "rm", f"{cname}-data", timeout=30)
    docker("volume", "rm", f"{cname}-varlib", timeout=30)
    inst = load_instances()
    inst.pop(name, None)
    save_instances(inst)
    # Delete-time stale-state sweep (manager-side mirror): the volume
    # rm already took the in-container keepalive marker, but the
    # selfwatch markers live in the agent data dir and would otherwise
    # be inherited by a recreated instance with the same name.
    # recoveries.json (node-wide total) and saved accounts are NOT
    # touched: counts are history, credentials are never deleted here.
    drop_selfwatch_state(name)
    return True


# Live resource resize (agent 1.0.31 — manager resize_local_instance parity):
# `docker update` applies new --memory/--cpus limits without restarting the
# container. The shrink-below-usage guard mirrors the manager: a limit below
# live RSS would OOM-kill the running session, so it is refused when usage
# is known (the 15s-cached docker stats); unknown usage fails open.
def resize_usage_ok(name, memory_mb):
    """True when a memory shrink stays above live usage. Never raises."""
    try:
        if memory_mb is None:
            return True, None
        used = instance_resources_all().get(name, {}).get("mem_used_mb")
        if used is None:
            return True, None
        if memory_mb < used:
            return False, (f"new memory limit {memory_mb} MB is below current "
                           f"usage {used:.0f} MB — stop the instance or pick a "
                           f"larger limit")
        return True, None
    except Exception:
        return True, None


def resize_instance_linux(name, memory_mb, cpus):
    """Live-update a container's CPU/RAM limits. Returns (ok, msg).

    Name re-validated; list-form docker() (no shell); state only persisted
    when docker accepts the limits. Never raises."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        return False, "invalid name"
    # agent-resize-numerics-sink-gate: coerce + bound memory_mb/cpus at
    # the sink (the resize route coerces + bounds today — memory
    # 1024-32768 MB, cpus 0.5-32, both-None refused — but the sink itself
    # took both verbatim into the docker update argv and the state
    # record, so a future internal caller passing an uncoercible,
    # negative, fractional-memory, or absurd value would drive
    # docker/state before any route gate ever ran). The sink envelope
    # matches the route's validator exactly. Coercion is
    # type-preserving for cpus: an int input stays int and a float
    # input stays float (only strings are coerced to float), so the
    # valid-path argv and state record are byte-identical to today
    # for every caller — the route passes float ("2.0"), direct int
    # callers pass int ("2"); a blanket float() coercion broke the
    # latter (t100 caught "1" becoming "1.0"). Memory coerces to int
    # (the route passes int). Bools rejected, fractional memory
    # rejected, NaN fails the bounds comparison, Infinity fails it too
    # (int(Inf) raises first and is caught). Fails closed before any
    # docker/state touch. The shrink-below-live-usage guard
    # (resize_usage_ok) stays route-only: it needs live docker stats
    # and the route already enforces it.
    if memory_mb is None and cpus is None:
        return False, "memory_mb or cpus required"
    cpus_was_int = False
    try:
        if isinstance(cpus, bool) or isinstance(memory_mb, bool):
            raise ValueError("bool numerics")
        if cpus is not None:
            cpus_was_int = isinstance(cpus, int)
            cpus = float(cpus)
        if memory_mb is not None:
            if isinstance(memory_mb, float) and not memory_mb.is_integer():
                raise ValueError("fractional memory")
            memory_mb = int(memory_mb)
    except (TypeError, ValueError, OverflowError):
        return False, "invalid cpus/memory"
    if memory_mb is not None and not (1024 <= memory_mb <= 32768):
        return False, "invalid cpus/memory"
    if cpus is not None and not (0.5 <= cpus <= 32):
        return False, "invalid cpus/memory"
    if cpus_was_int:
        cpus = int(cpus)
    args = ["update"]
    if memory_mb is not None:
        args += ["--memory", f"{memory_mb}m"]
    if cpus is not None:
        args += ["--cpus", str(cpus)]
    args.append(f"wd-{name}")
    rc, out = docker(*args, timeout=60)
    if rc != 0:
        return False, f"docker update failed: {out.strip()[-300:]}"
    inst = load_instances()
    if name in inst:
        if memory_mb is not None:
            inst[name]["memory_mb"] = memory_mb
        if cpus is not None:
            inst[name]["cpus"] = cpus
        save_instances(inst)
    invalidate_agent_status_cache(name)
    return True, "resized"


def instance_status_linux(name):
    # agent-status-delete-sink-gate: refuse invalid names outright at the
    # sink, returning the stopped-shaped dict without touching the
    # filesystem or docker. The /status route already 404s them, but the
    # /api/agent/instances loop calls cached_instance_status_linux() on
    # load_instances() keys with NO route gate between them, and invalid
    # names bypass that cache and call through here — so a corrupted
    # instances.json key reached this sink on every 10s manager poll,
    # building a volume path (existence probe of another instance's
    # markers via ../) and a docker inspect on wd-<name> before the
    # drift/degraded readers (which are gated) ever ran. Valid names
    # behave identically; the stopped shape matches the not-running
    # branch below exactly (no started_at, same as today).
    if not valid_agent_instance_name(name):
        return {"name": name, "running": False, "setup": "stopped", "roblox": False,
                "mem_used_mb": None, "cpu_pct": None,
                "roblox_drift": False, "roblox_drift_reason": None,
                "degraded": False, "degraded_reason": None}
    cname = f"wd-{name}"
    st = container_status(cname)
    if st != "running":
        return {"name": name, "running": False, "setup": "stopped", "roblox": False,
                "mem_used_mb": None, "cpu_pct": None,
                "roblox_drift": False, "roblox_drift_reason": None,
                "degraded": False, "degraded_reason": None}
    base = f"/var/lib/docker/volumes/{cname}-data/_data"
    started_at = 0
    try:
        rc, out = docker("inspect", "-f", "{{.State.StartedAt}}", cname, timeout=10)
        if rc == 0 and out.strip():
            import datetime
            dt = datetime.datetime.fromisoformat(out.strip().replace("Z", "+00:00"))
            started_at = int(dt.timestamp())
    except Exception:
        pass
    # check multiple possible locations (entrypoint may write to data/ directly)
    booted = any(os.path.exists(os.path.join(base, p)) for p in [
        "data/media/0/jerusalem/booted",
        "data/booted",
        "booted",
    ])
    roblox_done = any(os.path.exists(os.path.join(base, p)) for p in [
        "data/media/0/jerusalem/roblox_done",
        "data/roblox_done",
        "roblox_done",
    ])
    setup = "ready" if (booted and roblox_done) else ("installing_roblox" if booted else "booting")
    res = instance_resources_all().get(name, {})
    drift, drift_reason = read_roblox_drift(name)
    deg, deg_reason = read_selfwatch_degraded(name)
    return {"name": name, "running": True, "setup": setup, "roblox": roblox_done,
            "started_at": started_at,
            "mem_used_mb": res.get("mem_used_mb"), "cpu_pct": res.get("cpu_pct"),
            "roblox_drift": drift, "roblox_drift_reason": drift_reason,
            "degraded": deg, "degraded_reason": deg_reason}


# ---- Per-instance status snapshot cache (agent 1.0.20) ----------------------
# /api/agent/instances feeds the manager's every-10s remote fetch. Each poll
# ran `docker ps -a` + `docker inspect -f {{.State.StartedAt}}` once per
# instance — 2 serialized subprocess spawns per instance, on every poll,
# from every connected dashboard client. (Manager parity: the manager's own
# instance-health-cache, 2026-10-10, fixed the identical hot path locally.)
# The cache holds one snapshot per instance for 10s, matching the dashboard
# cadence. Mutation endpoints (create/start/stop/delete) invalidate the
# entry, so the next poll shows fresh state. Best-effort throughout: cache
# bugs must never break the response, so every helper never raises.
_agent_status_cache = {}  # name -> (epoch, snapshot dict)
_agent_status_lock = threading.Lock()
_AGENT_STATUS_TTL = 10.0
_AGENT_STATUS_PRUNE_AFTER = 60.0
_AGENT_STATUS_KEY_RE = r"[A-Za-z0-9_-]{1,32}"


def _agent_status_cache_key(name):
    """Cache key for an instance name, or None when the name is not a plain
    instance id. Non-string or invalid names bypass the cache (today's
    uncached behavior). Never raises."""
    try:
        if not isinstance(name, str):
            return None
        if re.fullmatch(_AGENT_STATUS_KEY_RE, name):
            return name
    except Exception:
        pass
    return None


def cached_instance_status_linux(name):
    """10s-TTL cached wrapper around instance_status_linux(). Never raises.

    Returns a fresh dict each call (callers can mutate it freely).
    Invalid names skip the cache and call through uncached; the sink
    itself now fails closed (stopped-shaped dict, no fs/docker touch),
    so the call-through is safe, not just uncached.
    """
    key = _agent_status_cache_key(name)
    if key is None:
        try:
            return instance_status_linux(name)
        except Exception:
            return {"name": name, "running": False, "setup": "stopped",
                    "roblox": False, "mem_used_mb": None, "cpu_pct": None,
                    "roblox_drift": False, "roblox_drift_reason": None}
    now = time.time()
    try:
        with _agent_status_lock:
            v = _agent_status_cache.get(key)
            if v is not None and now - v[0] <= _AGENT_STATUS_TTL:
                return dict(v[1])
    except Exception:
        pass
    try:
        data = instance_status_linux(name)
        if not isinstance(data, dict):
            data = {"name": name, "running": False, "setup": "stopped"}
    except Exception:
        data = {"name": name, "running": False, "setup": "stopped"}
    if not isinstance(data, dict):
        data = {"name": name, "running": False, "setup": "stopped"}
    try:
        with _agent_status_lock:
            _agent_status_cache[key] = (now, dict(data))
            for k in [k for k, (ts, _) in _agent_status_cache.items()
                      if now - ts > _AGENT_STATUS_PRUNE_AFTER]:
                _agent_status_cache.pop(k, None)
    except Exception:
        pass
    return dict(data)


def invalidate_agent_status_cache(name):
    """Drop one cached status snapshot (after create/start/stop/delete).
    Never raises."""
    try:
        key = _agent_status_cache_key(name)
        if key is not None:
            with _agent_status_lock:
                _agent_status_cache.pop(key, None)
    except Exception:
        pass


def sanitize_input_text(raw):
    """Make a string safe for `input text` through the input FIFO (which is
    eval'd by sh). Mirrors the manager's local sanitize: spaces -> %s
    (Android's input-text space encoding), everything shell-dangerous stripped."""
    txt = str(raw).replace(" ", "%s")
    txt = re.sub(r"[^a-zA-Z0-9%s.,!?@_-]", "", txt)
    return txt[:200]


# ---- b64 text staging (agent 1.0.38) ----------------------------------------
# The /input endpoint's b64:true branch used to run base64-decoded text
# through sanitize_input_text, which strips every character outside
# [a-zA-Z0-9%s.,!?@_-]. That silently mangled remote Roblox login
# passwords containing #, $, &, *, (, ), +, =, /, :, ;, <, >, [, ], {, },
# |, \, ", ', `, ~, ^ (the b64 flag exists precisely for such strings),
# while the manager's LOCAL login path types the full password via
# pwd.txt + `input text "$P"`. Result: a password that logs in fine on a
# local instance failed on a remote node with a generic "login failed"
# and no clue why.
#
# The b64 branch now stages the decoded text in a file inside the
# instance's data volume (600, atomic tmp+rename) and sends
# `input text "$(cat <file>)"` through the FIFO. The FIFO is eval'd by sh
# in the container, but the secret bytes never appear in the command
# line: inside double quotes, $(...) expansion results are not re-parsed,
# so $, backticks, quotes, ;, newlines etc. are inert data — exactly as
# safe as the manager's local pwd.txt approach, with identical `input
# text` semantics (including Android's %s -> space decoding, so
# local/remote behavior now matches).
# The staged file is deleted ~60s later by a daemon thread, and only when
# its content still matches what was staged (a newer stage is never
# removed early). Best-effort throughout; a staging failure makes the
# /input handler 500 visibly instead of typing a mangled password.
# Crash window (remote-b64-sweep): the daemon thread dies with the agent
# process, so an agent restart inside the 60s window — crash, self-update,
# systemd restart, host reboot — leaves the staged secret in the volume
# forever, readable from inside Android and from the host volume path
# (the same leak class the manager's login-pwd-cleanup closed locally).
# A thread-start failure leaves it behind too (the handler 500s, but the
# file is already staged). sweep_stale_b64_input_text() below closes
# both windows: main() sweeps unconditionally at startup (no live stage
# can exist yet — the server is not bound), and the selfwatch pass
# sweeps files older than 2x the TTL (a live stage is deleted by its own
# thread at 60s, so anything older is orphaned; the FIFO eval already
# happened long before, so the age gate cannot break a live typing).
_B64_TEXT_TTL_S = 60  # test seam: patch for fast delayed-delete tests
_B64_TEXT_NAME = "input-b64.txt"


def _agent_input_b64_path(name, vol_base=None):
    """Host volume path for the staged b64 text file. Name-gated; None
    when the name is invalid. Never raises."""
    if vol_base is None:
        vol_base = _STREAM_WANT_BASE_AGENT
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
            return None
        return os.path.join(vol_base, "wd-" + name + "-data", "_data",
                            "data", "media", "0", "jerusalem", _B64_TEXT_NAME)
    except Exception:
        return None


def _stage_b64_input_text(name, raw, vol_base=None):
    """Stage decoded b64 text for `input text`. Returns the FIFO-safe
    command string, or None when staging failed (the handler must 500,
    never fall back to a silently mangled password). Never raises."""
    try:
        txt = str(raw or "")
        if not txt:
            return None
        txt = txt[:200]  # same bound the sanitize path enforces
        path = _agent_input_b64_path(name, vol_base)
        if not path:
            return None
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            return None
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(txt)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return None
        staged = txt.encode("utf-8")

        def _cleanup():
            try:
                time.sleep(_B64_TEXT_TTL_S)
                try:
                    with open(path, "rb") as f:
                        cur = f.read()
                except Exception:
                    return
                if cur == staged:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
            except Exception:
                pass

        t = threading.Thread(target=_cleanup, daemon=True,
                             name="b64-text-cleanup")
        t.start()
        return 'input text "$(cat /data/media/0/jerusalem/%s)"' % _B64_TEXT_NAME
    except Exception:
        return None


def sweep_stale_b64_input_text(names=None, vol_base=None, max_age_s=None):
    """Remove stale b64 text staging files from instance volumes.

    Covers the crash window in _stage_b64_input_text: a daemon-thread
    cleanup dies with the agent process, and a thread-start failure
    leaves a staged file with no cleanup thread at all. Both the staged
    file and its atomic-write tmp sibling (a crash between the tmp write
    and os.replace leaves input-b64.txt.tmp behind) are removed.

    names=None reads the node instances (load_instances() keys); a dict
    contributes its keys, any other iterable contributes its items.
    max_age_s=None removes unconditionally (startup semantics: no live
    stage can exist before the server binds); otherwise only files older
    than max_age_s seconds are removed (selfwatch semantics: a live
    stage is at most 60s old when its cleanup thread fires, so an older
    file is orphaned, and the FIFO eval already happened). Returns the
    number of files removed. Best-effort, never raises; names are gated
    by _agent_input_b64_path, so a hand-edited instances.json can never
    turn this into a path-traversal delete.
    """
    removed = 0
    try:
        if names is None:
            try:
                inst = load_instances()
            except Exception:
                return 0
            names = list(inst.keys()) if isinstance(inst, dict) else []
        elif isinstance(names, dict):
            names = list(names.keys())
        else:
            names = list(names)
    except Exception:
        return 0
    for name in names:
        try:
            path = _agent_input_b64_path(name, vol_base=vol_base)
        except Exception:
            continue
        if not path:
            continue
        for p in (path, path + ".tmp"):
            try:
                if max_age_s is not None:
                    try:
                        age = time.time() - os.path.getmtime(p)
                    except OSError:
                        continue
                    if age <= max_age_s:
                        continue
                os.remove(p)
                removed += 1
            except OSError:
                pass
    return removed


def _agent_tap_cmd(data):
    """Build the `input tap ...` command for an /input tap payload.
    Returns the command string, or None for an invalid payload.
    Viewer contract is 0..1000 per axis (the dashboard clamps before
    sending). Non-finite coords (json accepts Infinity/NaN) and
    out-of-range coords are rejected: the old inline code converted
    with int() directly, and int(inf) raises OverflowError, which
    its except clause missed — so a crafted tap dropped the request
    with no response instead of a clean 400. NaN fails every
    comparison, inf fails the upper bound. Never raises."""
    try:
        fx = float(data.get("x"))
        fy = float(data.get("y"))
    except (TypeError, ValueError):
        return None
    if not (0 <= fx <= 1000 and 0 <= fy <= 1000):
        return None
    return (f"input tap {int(fx / 1000 * DISP_W)} "
            f"{int(fy / 1000 * DISP_H)}")


def _agent_text_cmd(name, data):
    """Build the `input ...` command for an /input text payload.
    Returns (cmd, http_code, error). b64:true payloads are staged via
    file (_stage_b64_input_text) so special characters survive intact;
    plain payloads keep the sanitize-strip. Never raises."""
    try:
        raw = data.get("text", "")
        if data.get("b64"):
            try:
                raw = base64.b64decode(str(raw)).decode(errors="replace")
            except Exception:
                raw = ""
            if not raw:
                return None, 400, "invalid input"
            cmd = _stage_b64_input_text(name, raw)
            if not cmd:
                return None, 500, "could not stage text input"
            return cmd, 200, ""
        txt = sanitize_input_text(raw)
        if not txt:
            return None, 400, "invalid input"
        return f"input text {txt}", 200, ""
    except Exception:
        return None, 400, "invalid input"


_shot_cache = {}  # name -> (mtime, jpeg_bytes)

# ---- adaptive screencap (agent 1.0.6) ---------------------------------------
# The manager polls /screenshot once per frame per viewer, so per-instance
# fetch recency == "a viewer is attached". While fetches are recent the
# stream_wanted marker stays fresh and the in-container loop captures fast
# (0.12s); when fetches stop, the marker goes stale and the loop idles at
# 2s, saving container CPU. Alert-only hint file: never raises, never
# affects health or the recovery budgets.
_SHOT_FETCH = {}  # name -> last successful-serve epoch
_SHOT_FETCH_LOCK = threading.Lock()
_STREAM_WANT_STALE_AGENT = 90  # selfwatch removes the marker after this long idle
_STREAM_WANT_BASE_AGENT = "/var/lib/docker/volumes"


def _agent_stream_want_path(name, vol_base=None):
    if vol_base is None:
        vol_base = _STREAM_WANT_BASE_AGENT
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
            return None
        return os.path.join(vol_base, "wd-" + name + "-data", "_data",
                            "data", "media", "0", "jerusalem", "stream_wanted")
    except Exception:
        return None


def _note_screenshot_fetch(name, vol_base=None):
    """Record viewer activity and keep the stream_wanted marker fresh."""
    if vol_base is None:
        vol_base = _STREAM_WANT_BASE_AGENT
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
            return
        with _SHOT_FETCH_LOCK:
            _SHOT_FETCH[name] = time.time()
        mp = _agent_stream_want_path(name, vol_base)
        if mp:
            try:
                os.makedirs(os.path.dirname(mp), exist_ok=True)
            except Exception:
                pass
            with open(mp, "a"):
                os.utime(mp, None)
    except Exception:
        pass


def _sweep_stream_want(name, vol_base=None):
    """Drop the marker when nobody fetched a screenshot recently."""
    if vol_base is None:
        vol_base = _STREAM_WANT_BASE_AGENT
    try:
        with _SHOT_FETCH_LOCK:
            last = _SHOT_FETCH.get(name, 0)
        if time.time() - last > _STREAM_WANT_STALE_AGENT:
            mp = _agent_stream_want_path(name, vol_base)
            if mp and os.path.exists(mp):
                os.remove(mp)
    except Exception:
        pass


# Stale-frame gate (stale-frame-gate): mirrors the manager's
# SCREENSHOT_STALE_SECS. A dead in-container screencap loop leaves
# live.png behind, and without this gate the agent served that frozen
# frame (via the mtime cache) forever — so the manager's remote MJPEG /
# H.264 / WebRTC frameless guards never fired for wing-node instances
# either, exactly like the local path. Same 10s threshold, same
# rationale: the loop captures every 0.12s with a viewer attached and
# every 2s idle, so 10s never gates a healthy loop.
SCREENSHOT_STALE_SECS = 10


def screenshot_bytes_linux(name):
    # 1.0.26 sink gate: refuse traversal names outright (defense in depth;
    # the route layer already 404s them).
    if not valid_agent_instance_name(name):
        return None
    cname = f"wd-{name}"
    base = f"/var/lib/docker/volumes/{cname}-data/_data"
    p = None
    for rel in ["data/media/0/jerusalem/live.png", "data/live.png", "live.png"]:
        cand = os.path.join(base, rel)
        if os.path.exists(cand):
            p = cand
            break
    if not p:
        return None
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return None
    # Stale-frame gate: never serve (or cache-hit) a frozen frame.
    if time.time() - mtime > SCREENSHOT_STALE_SECS:
        return None
    # Return cached JPEG if the PNG hasn't changed
    cached = _shot_cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        from PIL import Image
        import io
        img = Image.open(p)
        # Downscale to max 480px wide for fast streaming
        if img.width > 480:
            h = int(img.height * 480 / img.width)
            img = img.resize((480, h), Image.BILINEAR)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=60, optimize=True)
        data = buf.getvalue()
    except Exception:
        # Fallback: serve raw PNG if PIL unavailable
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return None
    _shot_cache[name] = (mtime, data)
    return data


def _find_fifo(cname):
    base = f"/var/lib/docker/volumes/{cname}-data/_data"
    for rel in ["data/media/0/jerusalem/input.fifo", "data/input.fifo", "input.fifo"]:
        p = os.path.join(base, rel)
        if os.path.exists(p):
            return p
    return os.path.join(base, "data/media/0/jerusalem/input.fifo")


def input_linux(name, cmd):
    # 1.0.26 sink gate: refuse traversal names outright (defense in depth;
    # the route layer already 404s them).
    if not valid_agent_instance_name(name):
        return False
    cname = f"wd-{name}"
    fifo = _find_fifo(cname)
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (cmd + "\n").encode())
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def roblox_focused_linux(name):
    """Is Roblox the focused app in this node instance? True/False/None.

    Same tri-state contract as the manager's roblox_focused(): True =
    dumpsys window's mCurrentFocus names com.roblox.client; False = the
    probe answered and focus is elsewhere (or no mCurrentFocus line on
    a successful probe); None = unknown (invalid name, probe error,
    timeout) — callers must NOT tap on unknown. Never raises.
    """
    if not valid_agent_instance_name(name):
        return None
    cname = "wd-" + name
    try:
        rc, out = docker("exec", cname, "waydroid", "shell", "dumpsys",
                         "window", timeout=15)
    except Exception:
        return None
    if rc != 0:
        return None
    for line in (out or "").splitlines():
        if "mCurrentFocus" in line:
            return "com.roblox.client" in line
    return False


def launch_roblox_linux(name):
    # 1.0.26 sink gate: refuse traversal names outright (defense in depth;
    # the route layer already 404s them).
    if not valid_agent_instance_name(name):
        return False
    # 1.0.41 focus gate (mirror of the manager's launch_roblox gate):
    # the manager fires this on every viewer open and on "Roblox all",
    # so an ungated tap yanked a running game out of focus. A focused
    # instance reports success with no taps; an unknown probe reports
    # failure with no taps (retryable, session-safe).
    focused = roblox_focused_linux(name)
    if focused is True:
        return True
    if focused is None:
        return False
    # Sequence matches the manager's verified local launch (HOME, drawer
    # swipe, tap at 129,424 on 1024x608); the swipe settle is sleep 2
    # there, not sleep 1.
    cmds = ["input keyevent 3", "sleep 1", "input swipe 512 400 512 100",
            "sleep 2", "input tap 129 424"]
    cname = f"wd-{name}"
    fifo = _find_fifo(cname)
    # ONE compound write, not one write per command: the input daemon
    # does `read cmd < $FIFO` (one line per FIFO open, reopening after
    # every line), so separate writes were lost once the writer closed —
    # remote "Launch Roblox" only ever executed HOME and never opened the
    # drawer or tapped the icon. This mirrors the manager's local
    # _send_input_cmds, which was fixed the same way (one compound write
    # per FIFO open).
    compound = "; ".join(cmds)
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (compound + "\n").encode())
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "WebServer/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, ctype="application/json"):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_bytes(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if self.path == "/api/agent/health":
            return True
        return _auth_key_ok(self.headers.get("X-Agent-Key", ""))

    def _check_auth(self):
        """'health' | 'ok' | 'ratelimited' | 'denied'. Never raises.

        /api/agent/health is unauthenticated by design (agent self-check +
        manager probes) and never counts toward the rate limit. On authed
        routes the rate limit is checked BEFORE the key compare, so a
        correct key presented while rate-limited still gets 429 (same
        429-before-credential ordering as the manager's /api/login). The
        limiter key is proxy-aware (_agent_client_ip): tunneled users get
        their own buckets instead of sharing the loopback peer's."""
        try:
            if self.path == "/api/agent/health":
                return "health"
            try:
                peer = self.client_address[0] if self.client_address else "?"
            except Exception:
                peer = "?"
            ip = _agent_client_ip(self.headers, peer)
            if not auth_allowed(ip):
                return "ratelimited"
            if self._authed():
                clear_auth_failures(ip)
                return "ok"
            record_auth_failure(ip)
            return "denied"
        except Exception:
            # Fail-open: a bookkeeping bug can never lock out the manager
            # (the key compare itself is already fail-closed inside
            # _auth_key_ok).
            return "ok"

    def _read_json(self, max_bytes=2097152):
        # Request-body cap (manager parity: manager/_read_json caps at the
        # same 2MB default). An over-cap Content-Length returns {} WITHOUT
        # reading the body — rfile.read(n) on a declared 10GB body would
        # otherwise page the whole thing into RAM (the agent's HTTP server
        # is reachable from the network, and the 60s socket timeout does
        # not bound total bytes read). HTTP/1.0 default protocol closes the
        # connection after the response, so skipping the read is safe.
        # Never raises: garbage lengths and malformed bodies fail closed.
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n < 0 or n > max_bytes:
                return {}
            parsed = json.loads(self.rfile.read(n).decode() or "{}")
            # Manager parity: a non-dict body (list/string/number/null)
            # would crash every caller's data.get(...) with AttributeError
            # and drop the connection with no response — coerce to {}.
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        _authv = self._check_auth()
        if _authv == "ratelimited":
            return self._send(429, {"error": "too many attempts"})
        if _authv == "denied":
            return self._send(401, {"error": "unauthorized"})
        if path == "/api/agent/health":
            h = {"ok": True, "version": VERSION,
                 "os": platform.system(),
                 "hostname": platform.node()}
            if not IS_WINDOWS:
                h["selfwatch"] = selfwatch_state()
            return self._send(200, h)
        if path == "/api/agent/stats":
            s = get_host_stats()
            s["instances"] = len(load_instances())
            return self._send(200, s)
        if path == "/api/agent/headroom":
            # Create-fleet plan preview (agent 1.0.21): host-level headroom
            # the manager's heartbeat stats don't carry. image_present
            # reuses the 1.0.18 create-preflight tri-state (True/False/None
            # fail-open, never raises); loop_devices reuses the 1.0.12
            # counter. Binder needs no plan-time accounting on wing nodes
            # (the agent self-heals the pool at create, 1.0.16).
            return self._send(200, {"ok": True,
                                    "image_present": sw_image_present(),
                                    "loop_devices": (None if IS_WINDOWS
                                                     else sw_count_loop_devices())})
        if path == "/api/agent/instances":
            if not require_linux():
                return self._send(200, {"instances": []})
            inst = load_instances()
            out = []
            for name, meta in inst.items():
                st = cached_instance_status_linux(name)
                st.update(meta)
                out.append(st)
            return self._send(200, {"instances": out})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/screenshot", path)
        if m:
            name = unquote(m.group(1))
            if not valid_agent_instance_name(name):
                return self._send(404, {"error": "instance not found"})
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            data = screenshot_bytes_linux(name)
            if data is None:
                return self._send(404, {"error": "no screenshot"})
            _note_screenshot_fetch(name)  # adaptive-screencap (1.0.6)
            return self._send_bytes(200, data, "image/png")
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/status", path)
        if m:
            name = unquote(m.group(1))
            if not valid_agent_instance_name(name):
                return self._send(404, {"error": "instance not found"})
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            return self._send(200, cached_instance_status_linux(name))
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/logs", path)
        if m:
            name = unquote(m.group(1))
            if not valid_agent_instance_name(name):
                return self._send(404, {"error": "instance not found"})
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            cname = f"wd-{name}"
            rc, out = docker("logs", "--tail", "50", cname, timeout=10)
            return self._send(200, {"logs": out[-4000:]})
        # dashboard "Eventos" modal: remote selfwatch events (agent 1.0.9)
        if path == "/api/agent/selfwatch-events":
            qs = dict(p.split("=", 1) for p in urlparse(self.path).query.split("&")
                      if "=" in p)
            return self._send(200, read_selfwatch_events(qs.get("lines", "50")))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        _authv = self._check_auth()
        if _authv == "ratelimited":
            return self._send(429, {"error": "too many attempts"})
        if _authv == "denied":
            return self._send(401, {"error": "unauthorized"})
        if path == "/api/agent/instances":
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            data = self._read_json()
            name = str(data.get("name", "")).strip()
            cpus = data.get("cpus", 2)
            memory_mb = data.get("memory_mb", 4096)
            ip = str(data.get("ip", "")).strip()
            if not name or not ip:
                return self._send(400, {"error": "name and ip required"})
            try:
                cpus = max(1, min(16, int(cpus)))
                memory_mb = max(512, min(32768, int(memory_mb)))
            except (TypeError, ValueError, OverflowError):
                # OverflowError: int(Infinity) from json.loads (manager
                # proxies create bodies through; same class as t148).
                return self._send(400, {"error": "invalid cpus/memory"})
            ok, msg = create_instance_linux(name, cpus, memory_mb, ip)
            if ok:
                invalidate_agent_status_cache(name)
            return self._send(200 if ok else 500, {"ok": ok, "msg": msg})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/start", path)
        if m:
            name = unquote(m.group(1))
            if not valid_agent_instance_name(name):
                return self._send(404, {"error": "instance not found"})
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            cname = f"wd-{name}"
            # A post-reboot start with a stock binderfs/loop pool wedges the
            # container (docker stubs the vanished bind sources). Best-effort
            # and guarded: a failure here must never block the start itself;
            # selfwatch owns any resulting wedge.
            try:
                _sw_prepare_pools_for_start()
            except Exception:
                pass
            rc, out = docker("start", cname, timeout=30)
            invalidate_agent_status_cache(name)
            return self._send(200 if rc == 0 else 500, {"ok": rc == 0, "msg": out[-500:]})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/input", path)
        if m:
            name = unquote(m.group(1))
            if not valid_agent_instance_name(name):
                return self._send(404, {"error": "instance not found"})
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            data = self._read_json()
            itype = data.get("type")
            cmd = None
            if itype == "tap":
                cmd = _agent_tap_cmd(data)
            elif itype == "key":
                keys = {"HOME": 3, "BACK": 4, "APP_SWITCH": 187}
                code = keys.get(str(data.get("key", "")).upper())
                if code is not None:
                    cmd = f"input keyevent {code}"
            elif itype == "text":
                # Text entry (e.g. Roblox login fields). Send {"text": "..."}
                # or {"text": "<base64>", "b64": true} when the string has
                # shell-special characters (1.0.38: staged via file so they
                # survive intact, matching the manager's local login path).
                cmd, code, err = _agent_text_cmd(name, data)
                if not cmd:
                    return self._send(code, {"error": err})
            if not cmd:
                return self._send(400, {"error": "invalid input"})
            ok = input_linux(name, cmd)
            return self._send(200 if ok else 500, {"ok": ok})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/launch-roblox", path)
        if m:
            name = unquote(m.group(1))
            if not valid_agent_instance_name(name):
                return self._send(404, {"error": "instance not found"})
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            ok = launch_roblox_linux(name)
            return self._send(200 if ok else 500, {"ok": ok})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/stop", path)
        if m:
            name = unquote(m.group(1))
            if not valid_agent_instance_name(name):
                return self._send(404, {"error": "instance not found"})
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            cname = f"wd-{name}"
            rc, _ = docker("stop", cname, timeout=60)
            invalidate_agent_status_cache(name)
            return self._send(200 if rc == 0 else 500, {"ok": rc == 0})
        # Live RAM/CPU resize (agent 1.0.31): docker update, no restart.
        # Bounds match the manager's create-time validation.
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/resize", path)
        if m:
            name = unquote(m.group(1))
            if not valid_agent_instance_name(name):
                return self._send(404, {"error": "instance not found"})
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            data = self._read_json()
            try:
                memory_mb = (int(data["memory_mb"])
                             if data.get("memory_mb") is not None else None)
                cpus = (float(data["cpus"])
                        if data.get("cpus") is not None else None)
            except (TypeError, ValueError, OverflowError):
                # OverflowError: int(Infinity) / float(huge int) from json.
                return self._send(400, {"error": "invalid cpus/memory"})
            if memory_mb is None and cpus is None:
                return self._send(400, {"error": "memory_mb or cpus required"})
            if memory_mb is not None and not (1024 <= memory_mb <= 32768):
                return self._send(400, {"error": "memory 1024-32768 MB"})
            if cpus is not None and not (0.5 <= cpus <= 32):
                return self._send(400, {"error": "cpus 0.5-32"})
            ok_usage, usage_err = resize_usage_ok(name, memory_mb)
            if not ok_usage:
                return self._send(400, {"error": usage_err})
            ok, msg = resize_instance_linux(name, memory_mb, cpus)
            if ok:
                # (agent 1.0.36) the 10s status snapshot would otherwise serve
                # the pre-resize state until TTL expiry; every other mutation
                # route invalidates, so the resize route does too.
                invalidate_agent_status_cache(name)
            return self._send(200 if ok else 500, {"ok": ok, "msg": msg})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        _authv = self._check_auth()
        if _authv == "ratelimited":
            return self._send(429, {"error": "too many attempts"})
        if _authv == "denied":
            return self._send(401, {"error": "unauthorized"})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)", path)
        if m:
            name = unquote(m.group(1))
            if not valid_agent_instance_name(name):
                return self._send(404, {"error": "instance not found"})
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            delete_instance_linux(name)
            invalidate_agent_status_cache(name)
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


# --- node self-watchdog (Linux only) ---------------------------------------
# Remote nodes have no cron watchdog: when a helper daemon dies inside a
# remote container (screencap loop, input daemon, roblox keepalive) nothing
# recovers it — black stream, dead input, Roblox sliding to the home screen.
# This in-agent thread runs the same recovery locally, mirroring
# df-watchdog.sh semantics: restart dead helpers / dead containers / stale
# screencap loops, with a circuit breaker that marks an instance DEGRADED
# (marker file, human-readable reason on the first line) when a recovery
# action fires too often, and auto-clears when all checks go green again.
SELF_WATCH_INTERVAL = 60
SELF_WATCH_DIR = os.path.join(DATA_DIR, "selfwatch")
_SELF_WATCH_HELPERS = ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh")
_SELF_WATCH_DF = "/data/media/0/jerusalem"

SELFWATCH_EVENTS_MAX = 200


def read_selfwatch_events(lines=100, st_dir=None):
    """Tail the selfwatch log for the dashboard Eventos modal.

    The log path is fixed (SELF_WATCH_DIR/selfwatch.log), never user input.
    Lines are returned RAW (untagged) — the manager applies its canonical
    watchdog_event_severity() so both sources render identically.
    Returns {"ok", "events": [{"line"}]}. Never raises; missing/unreadable
    log -> ok False and events=[] so the manager can skip the node honestly.
    """
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    try:
        n = max(1, min(int(lines), SELFWATCH_EVENTS_MAX))
    except (TypeError, ValueError):
        n = 100
    try:
        logf = os.path.join(st_dir, "selfwatch.log")
        with open(logf, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read().decode("utf-8", errors="replace")
        raw = chunk.splitlines()
        if size > 65536 and raw:
            raw = raw[1:]  # first line may be a partial read
        tail = raw[-n:]
        return {"ok": True, "events": [{"line": ln[:500]} for ln in tail]}
    except Exception:
        return {"ok": False, "events": []}


# Roblox version-drift guard (agent 1.0.5): remote nodes had no equivalent of
# the main-node watchdog's pinned-build check (df-watchdog.sh section 10),
# so a force-updated Roblox client on a remote instance was invisible until
# someone tapped into it. The selfwatch thread (every 60s) reads the
# installed versionName via `docker exec <c> waydroid shell dumpsys`
# (dumpsys exists only INSIDE the container — a host-side call would silently
# no-op) and writes <name>.roblox_drift (reason on the first line, the same
# marker convention as .degraded) when the build != PINNED_ROBLOX.
# Alert-only: no auto-remediation — a force-updated client needs a human
# decision (login tap coordinates + device-integrity assumptions are tuned
# to the pinned build). instance_status_linux() surfaces the marker so the
# manager's /api/instances remote branch and the dashboard drift badge pick
# it up with no further changes.
ROBLOX_PKG = "com.roblox.client"
PINNED_ROBLOX = os.environ.get("DF_PINNED_ROBLOX", "2.740.931")


def check_roblox_drift(name, st_dir=None, vol_base="/var/lib/docker/volumes"):
    """One drift pass for a node instance.

    Returns True when drifted, False when on the pinned build, None when
    skipped (invalid name, container not running, not booted, package absent,
    dumpsys failure). Surfacing only — no auto-remediation, no circuit-breaker
    budget. Never raises.
    """
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        return None
    cname = "wd-" + name
    try:
        if container_status(cname) != "running":
            return None
        base = os.path.join(vol_base, cname + "-data", "_data")
        booted = any(os.path.exists(os.path.join(base, p)) for p in [
            "data/media/0/jerusalem/booted",
            "data/booted",
            "booted",
        ])
        if not booted:
            return None
        rc, out = docker("exec", cname, "waydroid", "shell",
                         "dumpsys", "package", ROBLOX_PKG, timeout=30)
        if rc != 0:
            return None
        m = re.search(r"versionName=([^\s]+)", out or "")
        if not m:
            return None  # package absent or unexpected dumpsys output
        ver = m.group(1).strip()
        marker = os.path.join(st_dir, name + ".roblox_drift")
        seenf = os.path.join(st_dir, name + ".roblox_drift_seen")
        if ver != PINNED_ROBLOX:
            try:
                with open(marker, "w") as fh:
                    fh.write("Roblox client %s installed (pinned %s) - login coords + "
                             "integrity assumptions tuned to pinned build; "
                             "no auto-remediation\n" % (ver, PINNED_ROBLOX))
            except Exception:
                pass
            try:
                seen = open(seenf).read().strip() if os.path.exists(seenf) else ""
            except Exception:
                seen = ""
            if seen != ver:
                try:
                    with open(seenf, "w") as fh:
                        fh.write(ver + "\n")
                except Exception:
                    pass
                _sw_log(st_dir,
                        "ALERT %s Roblox client version DRIFT: installed %s (pinned %s) - "
                        "login coords + integrity assumptions tuned to pinned build; "
                        "no auto-remediation, needs human" % (name, ver, PINNED_ROBLOX))
            return True
        # back on the pinned build: clear marker + dedup state
        had = os.path.exists(marker)
        for f in (marker, seenf):
            try:
                os.remove(f)
            except Exception:
                pass
        if had:
            _sw_log(st_dir, "%s Roblox client back on pinned build %s"
                    % (name, PINNED_ROBLOX))
        return False
    except Exception:
        return None


def read_roblox_drift(name, st_dir=None):
    """Read the drift marker: (drifted bool, reason str|None). Never raises.

    Invalid names are rejected outright (same fullmatch gate as
    check_roblox_drift, which is the only writer): a collapsed name could
    otherwise map onto a different instance's marker.
    """
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        return False, None
    p = os.path.join(st_dir, name + ".roblox_drift")
    if not os.path.isfile(p):
        return False, None
    try:
        with open(p) as fh:
            line = fh.readline().strip()
        return True, (line[:160] if line else "Roblox client drifted off pinned build")
    except Exception:
        return False, None


def read_selfwatch_degraded(name, st_dir=None):
    """Read the selfwatch circuit-breaker marker: (degraded bool, reason str|None).

    The selfwatch thread writes <name>.degraded (reason on the first line,
    same convention as the main-node watchdog) when a recovery budget is
    exhausted and auto-recovery pauses. Surfaced per instance so the
    manager's /api/instances remote branch can show the same "needs
    attention" badge the local branch shows for the main-node watchdog.
    Never raises. Invalid names are rejected outright (same fullmatch gate
    as the selfwatch writer): a collapsed name could otherwise map onto a
    different instance's marker.
    """
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        return False, None
    p = os.path.join(st_dir, name + ".degraded")
    if not os.path.isfile(p):
        return False, None
    try:
        with open(p) as fh:
            line = fh.readline().strip()
        return True, (line[:160] if line else "auto-recovery paused")
    except Exception:
        return False, None


# --- Node disk/log self-recovery (agent 1.0.7) ---
# Remote nodes had no equivalent of the main-node watchdog's disk
# auto-recovery (df-watchdog.sh v6 section 8) or container-log trimmer (v10
# section 10): a full Docker data partition on a wing node kills every
# instance on it plus the agent's own heartbeats, and Docker's json-file
# driver never rotates — each instance container's *-json.log captures all
# Waydroid session stdout and can grow to tens of GB (69G was observed on
# the main node 2026-10-08). Both checks run ONCE per selfwatch pass
# (host-level, not per instance), Linux only. disk.degraded surfaces through
# the existing selfwatch_state() degraded map -> heartbeat -> dashboard
# Nodes "needs attention" badge, with zero manager/dashboard changes.
SW_DISK_PATH = "/var/lib/docker"      # filesystem whose usage gates pruning
SW_DISK_THRESHOLD_PCT = 90            # safe-prune at >= this
SW_DISK_PRUNE_BUDGET = 2              # >2 prunes/hour w/o relief -> disk.degraded
SW_LOG_GLOB = "/var/lib/docker/containers/*/*-json.log"


def _sw_env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


SW_LOG_TRIM_MAX = _sw_env_int("DF_LOG_TRIM_MAX", 1073741824)    # trim logs over 1GB...
SW_LOG_TRIM_KEEP = _sw_env_int("DF_LOG_TRIM_KEEP", 1073741824)  # ...keeping the last 1GB

# --- memory-pressure guard (agent 1.0.13) -----------------------------------
# df-watchdog.sh v7 watches MemAvailable on the main node; wing nodes had no
# equivalent — an OOM-pressure node had its containers killed and the
# selfwatch just restarted them, burning the container-start circuit budget
# until a misleading "...restarted N times" DEGRADED, with the real cause
# (memory pressure) never surfaced. Same alert-only semantics as the main
# node: MemAvailable below SW_MEM_LOW_KB on SW_MEM_LOW_TRIPS checks inside
# the sliding window -> mem.degraded marker (reason on the first line, same
# convention as disk.degraded/loop.degraded) -> selfwatch_state() degraded
# map -> heartbeat -> dashboard Nodes "needs attention" badge, zero
# manager/dashboard changes; fresh "Out of memory" dmesg lines are surfaced
# too. ALERT-ONLY: nothing is ever stopped/killed/restarted by this guard.
SW_PROC_MEMINFO = "/proc/meminfo"   # path read for MemAvailable
SW_MEM_LOW_KB = 1048576             # <1 GiB MemAvailable = low (v7 parity)
SW_MEM_LOW_WINDOW = 3600            # low readings live in a 1h window
SW_MEM_LOW_TRIPS = 3                # 3 low readings in the window -> DEGRADED
SW_MEM_OOM_WINDOW = 86400           # OOM event dedup window (24h)


def sw_disk_usage_pct(disk_path=SW_DISK_PATH):
    """Used % of the filesystem holding disk_path, or None when unreadable.
    Never raises."""
    try:
        u = shutil.disk_usage(disk_path)
        if not u.total:
            return None
        pct = u.used * 100 // u.total
        return max(0, min(100, pct))
    except Exception:
        return None


def sw_check_disk(st_dir=None, disk_path=SW_DISK_PATH, docker_down=False):
    """Host-level disk-pressure guard. At >=90% on the docker data
    filesystem: SAFE prune only (dangling images + build cache — NEVER
    volume prune or `image prune -a`; instance data lives in named volumes
    + the instance image). Budgeted: >2 prunes/hour without relief ->
    disk.degraded (reason on first line; auto-prune pauses, alert-only)
    until usage drops below 90%, which clears it. Never raises.
    docker_down (agent 1.0.39): while the daemon guard reports dockerd
    down, the alert WARN still fires but the prune attempt is skipped —
    pruning against a dead daemon cannot help and must not burn the
    disk budget."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    try:
        os.makedirs(st_dir, exist_ok=True)
    except Exception:
        pass
    try:
        pct = sw_disk_usage_pct(disk_path)
    except Exception:
        return
    if pct is None:
        return
    marker = os.path.join(st_dir, "disk.degraded")
    ev_key = "disk.prune"
    if pct < SW_DISK_THRESHOLD_PCT:
        if os.path.exists(marker):
            for f in (marker, os.path.join(st_dir, ev_key + ".ev")):
                try:
                    os.remove(f)
                except Exception:
                    pass
            _sw_log(st_dir, "disk pressure relieved (%d%% on %s) - auto-prune resumed"
                    % (pct, disk_path))
        return
    if os.path.exists(marker):
        _sw_log(st_dir, "WARN disk %d%% on %s - auto-prune paused (budget exhausted, needs human)"
                % (pct, disk_path))
        return
    if docker_down:
        _sw_log(st_dir, "WARN disk %d%% on %s - auto-prune skipped (docker daemon down)"
                % (pct, disk_path))
        return
    n = _sw_note_event(st_dir, ev_key, 3600)
    if n > SW_DISK_PRUNE_BUDGET:
        try:
            with open(marker, "w") as fh:
                fh.write("disk auto-prune ran %d times in last 3600s without relieving pressure - auto-prune paused\n" % n)
        except Exception:
            pass
        _sw_log(st_dir, "disk DEGRADED: auto-prune ran %dx this hour, still %d%% - paused (needs human)"
                % (n, pct))
        return
    freed = "?"
    prune_ok = False
    try:
        rc, out = docker("image", "prune", "-f", timeout=120)
        prune_ok = (rc == 0)
        m = re.search(r"(?i)total reclaimed[^,\n]*", out or "")
        freed = m.group(0).strip() if m else ("rc=%d" % rc)
    except Exception as e:
        freed = "prune-error: %s" % str(e)[:60]
    try:
        docker("builder", "prune", "-f", timeout=120)
    except Exception:
        pass
    try:
        after = sw_disk_usage_pct(disk_path)
    except Exception:
        after = None
    _sw_log(st_dir, "disk %d%% -> safe prune (dangling images + build cache, %d/%d per hour): %s; now %s"
            % (pct, n, SW_DISK_PRUNE_BUDGET, freed,
               ("%d%%" % after) if after is not None else "?"))
    if prune_ok:
        _sw_bump_recoveries(st_dir)


SW_LOOP_DEVICE_COUNT = 64  # loop pool provisioning target for the fleet
SW_LOOP_DEVICE_FLOOR = 60  # below this the pool is considered degraded


def sw_count_loop_devices(loop_dir="/dev"):
    """Number of loopN device nodes in loop_dir. Name-pattern match only
    (excludes loop-control and loopNpM partition nodes, like the manager's
    ls-based counter). Best-effort, never raises."""
    try:
        return sum(1 for fn in os.listdir(loop_dir)
                   if re.fullmatch(r"loop\d+", fn))
    except Exception:
        return 0


def sw_ensure_loop_devices(st_dir=None, loop_dir="/dev", sys_base="/sys/module"):
    """Host-level loop-device guard (agent 1.0.12).

    Each booted Waydroid instance consumes ~2 loop devices (image mounts);
    the 2026-10-08 main-node recovery needed ~40 for 20 instances. A fresh
    wing node exposes only the stock set (8) after a reboot unless the pool
    was provisioned persistently (install-wing.sh: `options loop
    max_loop=64` in /etc/modprobe.d/loop.conf + `loop` in /etc/modules) —
    and devtmpfs quirks can leave nodes missing even with a loaded module.

    When fewer than SW_LOOP_DEVICE_FLOOR nodes exist: load the loop module
    with max_loop=SW_LOOP_DEVICE_COUNT if it isn't loaded yet (modprobe on
    an already-loaded module is a harmless no-op — max_loop cannot change
    on a live module), then mknod any missing nodes. A live module whose
    max_loop parameter reads below the floor makes extra nodes useless
    (mknod cannot grow the driver's table) — that reports False honestly.

    Failure -> loop.degraded marker (reason on the first line, same
    convention as disk.degraded) -> the existing selfwatch_state() degraded
    map -> heartbeat -> dashboard Nodes "needs attention" badge, with zero
    manager/dashboard changes. Relief clears the marker with a log line.
    Returns True when the pool is at/above floor afterwards.
    Best-effort, never raises; no user input interpolated (ints only)."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    try:
        os.makedirs(st_dir, exist_ok=True)
    except Exception:
        pass
    marker = os.path.join(st_dir, "loop.degraded")
    try:
        n = sw_count_loop_devices(loop_dir)
    except Exception:
        return False
    if n >= SW_LOOP_DEVICE_FLOOR:
        if os.path.exists(marker):
            for f in (marker, os.path.join(st_dir, "loop.ensure.ev")):
                try:
                    os.remove(f)
                except Exception:
                    pass
            _sw_log(st_dir, "loop device pool relieved (%d nodes) - guard resumed" % n)
        return True
    if os.path.exists(marker):
        _sw_log(st_dir, "WARN loop device pool %d<%d - auto-provision paused (budget exhausted, needs human)"
                % (n, SW_LOOP_DEVICE_FLOOR))
        return False
    reason = None
    try:
        rc, _ = run(["test", "-d", os.path.join(sys_base, "loop")], timeout=10)
        if rc != 0:
            # Module not loaded at all: load it with the fleet-sized pool.
            run(["modprobe", "loop", "max_loop=%d" % SW_LOOP_DEVICE_COUNT], timeout=15)
        try:
            present = {fn for fn in os.listdir(loop_dir)
                       if re.fullmatch(r"loop\d+", fn)}
        except Exception:
            present = set()
        for i in range(SW_LOOP_DEVICE_COUNT):
            if ("loop%d" % i) not in present:
                run(["mknod", os.path.join(loop_dir, "loop%d" % i),
                     "b", "7", str(i)], timeout=10)
        n = sw_count_loop_devices(loop_dir)
        if n >= SW_LOOP_DEVICE_FLOOR:
            # Honesty check: nodes beyond the driver's max_loop are unusable.
            rc, out = run(["cat", os.path.join(sys_base, "loop", "parameters", "max_loop")],
                          timeout=10)
            if rc == 0:
                try:
                    if int(out.strip()) < SW_LOOP_DEVICE_FLOOR:
                        reason = ("loop driver max_loop=%s below floor %d - "
                                  "nodes exist but are unusable (needs host fix)"
                                  % (out.strip(), SW_LOOP_DEVICE_FLOOR))
                except Exception:
                    pass
        else:
            reason = ("loop device pool %d<%d after provisioning - needs human"
                      % (n, SW_LOOP_DEVICE_FLOOR))
    except Exception as e:
        reason = "loop device provisioning failed (%s)" % str(e)[:60]
    if reason is None:
        _sw_log(st_dir, "loop device pool provisioned to %d nodes" % n)
        _sw_bump_recoveries(st_dir)
        return True
    try:
        with open(marker, "w") as fh:
            fh.write(reason + "\n")
    except Exception:
        pass
    _sw_log(st_dir, "loop DEGRADED: %s" % reason)
    return False


def sw_read_mem_kb(meminfo_path=SW_PROC_MEMINFO):
    """Parse MemAvailable (kB) from a meminfo-style file, or None when
    unreadable/absent/malformed. Best-effort, never raises."""
    try:
        with open(meminfo_path) as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1])
                    return None
    except Exception:
        return None
    return None


def sw_oom_count(dmesg_text=None):
    """Count 'Out of memory' lines (case-insensitive), the same idiom as the
    main-node watchdog's `grep -ci`. dmesg_text=None reads live dmesg output;
    None when the count can't be determined. Never raises."""
    try:
        if dmesg_text is None:
            rc, out = run(["dmesg"], timeout=15)
            if rc != 0:
                return None
            dmesg_text = out
        return sum(1 for line in (dmesg_text or "").splitlines()
                   if "out of memory" in line.lower())
    except Exception:
        return None


def sw_oom_cursor_read(st_dir):
    """The last-seen cumulative OOM count (0 when missing/corrupt)."""
    try:
        with open(os.path.join(st_dir, "oom.count")) as fh:
            n = int(fh.read().strip())
            return n if n >= 0 else 0
    except Exception:
        return 0


def sw_check_memory(st_dir=None, meminfo_path=SW_PROC_MEMINFO, dmesg_text=None):
    """Host-level memory-pressure guard (agent 1.0.13), mirroring
    df-watchdog.sh v7 section 9. ALERT-ONLY: the guard only writes markers
    and log lines; it never stops, kills, or restarts anything.

    Low-memory path: MemAvailable < SW_MEM_LOW_KB is an event in the
    SW_MEM_LOW_WINDOW sliding window; SW_MEM_LOW_TRIPS such readings ->
    mem.degraded marker (reason on the first line) and a single DEGRADED
    log line; relief (>= SW_MEM_LOW_KB) removes the marker + event file.
    OOM path: fresh "Out of memory" dmesg lines since the cursor are
    surfaced as one WARN per pass; the cursor file always advances.
    Surfaces through the existing selfwatch_state() degraded map ->
    heartbeat -> dashboard Nodes "needs attention" badge, zero
    manager/dashboard changes. Best-effort, never raises."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    try:
        os.makedirs(st_dir, exist_ok=True)
    except Exception:
        pass
    marker = os.path.join(st_dir, "mem.degraded")
    memkb = sw_read_mem_kb(meminfo_path)
    if memkb is not None:
        if memkb >= SW_MEM_LOW_KB:
            if os.path.exists(marker):
                for f in (marker, os.path.join(st_dir, "mem.low.ev")):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
                _sw_log(st_dir, "memory pressure relieved (%dMB available) - alerts resumed"
                        % (memkb // 1024))
        elif os.path.exists(marker):
            pass  # silent while degraded (v7 parity: no repeat spam)
        else:
            try:
                n = _sw_note_event(st_dir, "mem.low", SW_MEM_LOW_WINDOW)
            except Exception:
                n = 0
            if n >= SW_MEM_LOW_TRIPS:
                try:
                    with open(marker, "w") as fh:
                        fh.write("host MemAvailable %dMB - instance limits overcommit RAM (needs human)\n"
                                 % (memkb // 1024))
                except Exception:
                    pass
                _sw_log(st_dir, "memory DEGRADED: MemAvailable %dMB seen %dx this hour - paused (needs human)"
                        % (memkb // 1024, n))
            else:
                _sw_log(st_dir, "WARN memory pressure: MemAvailable %dMB (report only, %d/%d this hour)"
                        % (memkb // 1024, n, SW_MEM_LOW_TRIPS))
    # OOM scan runs every pass regardless of the low-memory state, like v7.
    count = sw_oom_count(dmesg_text)
    if count is not None:
        prev = sw_oom_cursor_read(st_dir)
        if count > prev:
            try:
                _sw_note_event(st_dir, "mem.oom", SW_MEM_OOM_WINDOW)
            except Exception:
                pass
            _sw_log(st_dir, "WARN OOM killer fired %dx since last check (report only - an instance may be dead, check docker ps)"
                    % (count - prev))
        try:
            with open(os.path.join(st_dir, "oom.count"), "w") as fh:
                fh.write("%d\n" % count)
        except Exception:
            pass


def sw_trim_container_logs(st_dir=None, log_glob=None, trim_max=None, trim_keep=None):
    """Host-level Docker json-log trimmer. Any *-json.log over trim_max is
    rewritten in place to its last trim_keep bytes (same inode — dockerd's
    open fd stays valid; the json-file driver appends at EOF so logging
    continues). Never touches instance data volumes. Self-limiting; a
    failed trim logs one WARN. Never raises."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    if log_glob is None:
        log_glob = SW_LOG_GLOB
    if trim_max is None:
        trim_max = SW_LOG_TRIM_MAX
    if trim_keep is None:
        trim_keep = SW_LOG_TRIM_KEEP
    # agent-sw-numerics-sink-gate: coerce + bound both byte counts at the
    # sink, before any file touch. Invalid values fail closed with one
    # WARN per day (event-window deduped, never silent, never spammy):
    # a negative trim_keep used to rewrite logs to zero bytes, and a
    # negative trim_max made every log "over threshold" every pass.
    trim_max = _sw_coerce_trim_bytes(trim_max)
    trim_keep = _sw_coerce_trim_bytes(trim_keep)
    if trim_max is None or trim_keep is None:
        try:
            os.makedirs(st_dir, exist_ok=True)
            if _sw_note_event(st_dir, "log.trim.badconfig", 86400) == 1:
                _sw_log(st_dir, "WARN log-trim: invalid trim_max/trim_keep, "
                        "skipping trim (check DF_LOG_TRIM_MAX/DF_LOG_TRIM_KEEP)")
        except Exception:
            pass
        return
    try:
        paths = sorted(glob.glob(log_glob))
    except Exception:
        return
    for p in paths:
        try:
            if not os.path.isfile(p):
                continue
            size = os.path.getsize(p)
            if size <= trim_max:
                continue
            if trim_keep >= size:
                # Keeping >= the whole file is a no-op, not a trim: the
                # old code seek(-trim_keep, SEEK_END) before the file
                # start here and WARNed every pass forever.
                continue
            with open(p, "r+b") as fh:
                fh.seek(-trim_keep, os.SEEK_END)
                tail = fh.read()
                fh.seek(0)
                fh.write(tail)
                fh.truncate()
            _sw_log(st_dir, "log-trim: trimmed %s *-json.log %d -> %d bytes (in-place, fd-safe)"
                    % (os.path.basename(os.path.dirname(p)), size, len(tail)))
            _sw_note_event(st_dir, "log.trim", 86400)
            _sw_bump_recoveries(st_dir)
        except Exception as e:
            _sw_log(st_dir, "WARN log-trim: failed to trim %s (%s)" % (p, str(e)[:80]))


# --- boot recovery: host-boot fleet recovery (agent 1.0.32) ---
# Main-node parity with df-watchdog.sh v14 (section 0): the agent's selfwatch
# had the same post-host-reboot gap on wing nodes — a reboot stops every
# wd-* container (docker run sets no --restart policy) and drops the
# binderfs pool back to the stock set (only the CREATE path ever restored it,
# agent 1.0.16). The selfwatch container-down check then fired for EVERY
# stopped instance in the same pass: a Waydroid boot storm on the wing box,
# and `docker start` on containers whose bind-mounted /dev/binderfs/binderN
# sources had vanished — docker auto-creates empty directories at missing
# bind sources, so Waydroid booted with dead binder mounts, wedged at
# stuck-boot, and burned the 6/hour container-start circuit budget into mass
# DEGRADED. This guard runs first in every selfwatch pass while a fresh boot
# is unrecovered: it (1) restores the binderfs pool (mount if needed +
# binder-control writes; empty docker stub dirs are rmdir'd — rmdir removes
# ONLY empty dirs so it can never delete data — and recreated; non-empty
# stubs are left alone with one ALERT per boot and the fleet start paused),
# (2) stagger-starts stopped instances (SW_BOOT_MAX_STARTS `docker start`s
# per 60s selfwatch pass, sorted order, .degraded skipped, per-instance
# SW_BOOT_ATTEMPTS budget before DEGRADED with the reason on the marker's
# first line -> selfwatch_state() degraded map -> heartbeat -> dashboard
# Nodes "needs attention" badge, zero manager/dashboard changes),
# (3) writes the sentinel when the queue drains. While active,
# selfwatch_check_instance's container-down branch defers its own starts (no
# race, no herd). Missed window (uptime >= window, e.g. the agent was down
# across the boot): sentinel written once, normal checks own recovery —
# today's behavior, no regression. Unreadable uptime: fail open, retry next
# cycle. Linux only (selfwatch itself is Linux-only); every helper
# best-effort, never raises. Never deletes instance data, credentials, or
# tokens; never stops/kills/restarts anything except `docker start` on
# stopped containers.
_SW_BOOT_SENTINEL_NAME = "wing-boot.done"
SW_BOOT_TMP = os.environ.get("DF_BOOT_TMP", "/tmp")   # wiped by reboot
SW_PROC_UPTIME = os.environ.get("DF_PROC_UPTIME", "/proc/uptime")
SW_BINDERFS = os.environ.get("DF_BINDERFS", "/dev/binderfs")


def _sw_env_int_min(name, default, minv):
    """_sw_env_int with a floor: values below minv (and garbage, which
    _sw_env_int already degrades to default) fall back to default. A 0 or
    negative stagger budget would either wedge the queue or recreate the
    herd, so fail safe to the default. Never raises."""
    try:
        v = _sw_env_int(name, default)
        if v < minv:
            return default
        return v
    except Exception:
        return default


SW_BOOT_WINDOW_S = _sw_env_int_min("DF_BOOT_WINDOW_S", 900, 1)
SW_BOOT_MAX_STARTS = _sw_env_int_min("DF_BOOT_MAX_STARTS", 3, 1)
SW_BOOT_ATTEMPTS = _sw_env_int_min("DF_BOOT_ATTEMPTS", 3, 1)
SW_BINDER_COUNT = _sw_env_int_min("DF_BINDER_COUNT", 64, 1)


def _sw_boot_sentinel_path(sentinel_dir=None):
    return os.path.join(sentinel_dir or SW_BOOT_TMP, _SW_BOOT_SENTINEL_NAME)


def _sw_read_uptime_s(uptime_path=None):
    """Host uptime in whole seconds, or None when unreadable. Never raises."""
    try:
        with open(uptime_path or SW_PROC_UPTIME) as fh:
            parts = fh.read(64).split()
        if not parts:
            return None
        return int(float(parts[0]))
    except Exception:
        return None


def _sw_write_boot_sentinel(sentinel_dir=None):
    try:
        d = sentinel_dir or SW_BOOT_TMP
        os.makedirs(d, exist_ok=True)
        with open(_sw_boot_sentinel_path(sentinel_dir), "a"):
            pass
    except Exception:
        pass


def sw_boot_recovery_active(sentinel_dir=None, uptime_path=None, window_s=None):
    """True while a fresh host boot still needs fleet recovery. Never raises.

    Test seam for the selfwatch container-down deferral: the sentinel lives
    in a tmp dir wiped by reboot (/tmp by default), so a fresh boot means
    sentinel missing + host uptime under the boot window. Unreadable uptime
    fails open to False (no recovery storm on a broken clock)."""
    try:
        if os.path.exists(_sw_boot_sentinel_path(sentinel_dir)):
            return False
        up = _sw_read_uptime_s(uptime_path)
        if up is None:
            return False
        return up < (SW_BOOT_WINDOW_S if window_s is None else window_s)
    except Exception:
        return False


def sw_boot_binder_pool(st_dir=None, binderfs=None, count=None, run_fn=None,
                        alert_dir=None):
    """Restore the binderfs device pool after a host reboot.

    Returns (have, stub_blocked): have = device count, stub_blocked = True
    when a non-empty docker bind-stub directory blocked a device name (one
    ALERT per boot, fleet start paused until human cleanup). Empty stub
    dirs are rmdir'd — rmdir removes ONLY empty dirs so it can never delete
    data — and recreated via binder-control. A rejected binder-control write
    leaves the count short (fleet start paused, retry next cycle).
    alert_dir (default SW_BOOT_TMP) holds the per-boot stub-alert dedup
    file — it must live on a tmpfs wiped by reboot. Never raises."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    binderfs = binderfs or SW_BINDERFS
    count = SW_BINDER_COUNT if count is None else count
    # agent-binder-count-sink-gate: same bound as sw_ensure_binder_devices
    # (was no coercion and no ceiling at all — range(count) drove the
    # binder-control write loop, so a bad DF_BINDER_COUNT env value would
    # loop it every selfwatch pass). Fails closed to (0, False) with zero
    # shell touches; callers treat have < SW_BINDER_COUNT as paused.
    count = _sw_coerce_binder_count(count)
    if count is None:
        return 0, False
    run_fn = run_fn or run
    alert_dir = alert_dir or SW_BOOT_TMP
    control = os.path.join(binderfs, "binder-control")
    try:
        if not os.path.exists(control):
            run_fn("modprobe binder_linux 2>/dev/null", timeout=15, shell=True)
            run_fn("mkdir -p %s && mount -t binder binder %s 2>/dev/null"
                   % (binderfs, binderfs), timeout=15, shell=True)
            if not os.path.exists(control):
                return 0, False
        have = 0
        stub_blocked = False
        for i in range(count):
            name = "binder" if i == 0 else "binder%d" % i
            path = os.path.join(binderfs, name)
            skip_write = False
            if os.path.isdir(path):
                # docker auto-creates empty directories at missing bind
                # sources on `docker start`. rmdir removes ONLY empty dirs;
                # a populated one is left alone with one ALERT per boot.
                try:
                    os.rmdir(path)
                except Exception:
                    skip_write = True
                    stub_blocked = True
                else:
                    _sw_log(st_dir, "boot recovery: removed empty docker bind-stub dir %s" % name)
            if skip_write:
                alertf = os.path.join(alert_dir, "wing-boot.stub-alert")
                try:
                    if not os.path.exists(alertf):
                        with open(alertf, "w") as fh:
                            fh.write("1\n")
                        _sw_log(st_dir, "ALERT boot recovery: %s is a non-empty directory (not a binder device) - manual cleanup needed, fleet start paused" % path)
                except Exception:
                    pass
                continue
            if os.path.exists(path):
                have += 1
            else:
                rc, _ = run_fn("echo %s > %s 2>/dev/null" % (name, control),
                               timeout=10, shell=True)
                if rc == 0:
                    have += 1
        return have, stub_blocked
    except Exception:
        return 0, False


def _sw_prepare_pools_for_start():
    """Restore the binderfs + loop device pools before `docker start`.

    Same post-reboot hazard as the manager's _prepare_pools_for_start: a
    raw `docker start` with a stock binderfs pool makes docker auto-create
    empty stub dirs at the vanished bind sources -> dead binder mounts ->
    stuck-boot wedge. The selfwatch boot recovery (1.0.32 section 0)
    repairs this in its own 60s path, but the /start endpoint (dashboard
    Start/Restart, batch "Start all") calls `docker start` directly — a
    start issued before or during boot recovery would wedge the instance.

    Fast path: a single listdir on SW_BINDERFS; a complete pool costs zero
    subprocesses. Otherwise sw_boot_binder_pool() restores the pool (it
    also rmdirs empty docker bind-stub dirs; never raises). Loop devices
    are topped up via sw_ensure_loop_devices() when below
    SW_LOOP_DEVICE_FLOOR. Best-effort, never raises — on any failure the
    caller proceeds with the old behavior and selfwatch owns the wedge.
    """
    try:
        try:
            names = set(os.listdir(SW_BINDERFS))
        except OSError:
            names = None
        if names is None:
            # Cannot even list the dir: let the full restore path try.
            sw_boot_binder_pool()
        else:
            # agent-binder-count-sink-gate: build the expected set from
            # the gated count — an absurd DF_BINDER_COUNT must not drive
            # an unbounded set build here. On a gated-out count the
            # restore call below fails closed on the same gate.
            pool_n = _sw_coerce_binder_count(SW_BINDER_COUNT)
            if pool_n is None:
                sw_boot_binder_pool()
            else:
                expected = {"binder"} | {"binder%d" % i
                                         for i in range(1, pool_n)}
                missing = [n for n in expected if n not in names]
                stubs = [n for n in expected
                         if n in names and os.path.isdir(os.path.join(SW_BINDERFS, n))]
                if missing or stubs:
                    # sw_boot_binder_pool() also rmdirs empty docker bind-stub
                    # dirs and recreates the devices; never raises.
                    sw_boot_binder_pool()
        try:
            if sw_count_loop_devices() < SW_LOOP_DEVICE_FLOOR:
                sw_ensure_loop_devices()
        except Exception:
            pass
    except Exception:
        pass


def sw_boot_recovery(st_dir=None, sentinel_dir=None, uptime_path=None,
                     binderfs=None, instances=None,
                     docker_fn=None, run_fn=None):
    """One boot-recovery pass. Returns a status string; never raises.

    Statuses: "noop-sentinel" (already recovered), "unreadable" (uptime
    unreadable — fail open, retry next pass), "noop-window" (missed the boot
    window — sentinel written, normal per-instance checks own recovery),
    "pool-paused" (binderfs unavailable or a non-empty stub blocks the pool —
    fleet start paused this pass), "incomplete" (pool short or docker ps
    failed — retry next pass), "progress" (stagger ongoing),
    "complete" (queue drained, sentinel written).

    Boot-recovery pause surfacing (main-node watchdog v16 parity): a
    paused recovery used to be log-only — the Nodes modal showed plain
    "stopped" cards while the node's fleet start was paused. The paused
    paths now write st_dir/boot.degraded (reason on the first line), which
    selfwatch_state() already maps into the heartbeat degraded map, so it
    surfaces in the Nodes "needs attention" badge and the Nodos-button
    badge with zero manager/dashboard changes. The marker clears as soon
    as the pool is complete again, when the queue drains, and when the
    boot window is missed — it never outlives the pause. No agent
    VERSION bump: this writes a marker file only (no new recovery
    action, no protocol change — the degraded map already carries
    arbitrary keys, and the manager's auto-login gate is untouched)."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    try:
        sent = _sw_boot_sentinel_path(sentinel_dir)
        if os.path.exists(sent):
            try:
                os.remove(os.path.join(st_dir, "boot.degraded"))
            except Exception:
                pass
            return "noop-sentinel"
        up = _sw_read_uptime_s(uptime_path)
        if up is None:
            return "unreadable"
        if up >= SW_BOOT_WINDOW_S:
            try:
                os.remove(os.path.join(st_dir, "boot.degraded"))
            except Exception:
                pass
            _sw_write_boot_sentinel(sentinel_dir)
            _sw_log(st_dir, "boot recovery: boot window missed (uptime %ds) - normal per-instance checks own recovery" % up)
            return "noop-window"
        _sw_log(st_dir, "boot recovery: fresh host boot (uptime %ds) - restoring binder pool, starting fleet staggered" % up)
        have, stub_blocked = sw_boot_binder_pool(
            st_dir=st_dir, binderfs=binderfs, run_fn=run_fn,
            alert_dir=sentinel_dir)
        _sw_log(st_dir, "boot recovery: binder pool at %d/%d devices"
                % (have, SW_BINDER_COUNT))
        if stub_blocked:
            try:
                with open(os.path.join(st_dir, "boot.degraded"), "w") as fh:
                    fh.write("boot recovery paused: binder pool blocked by a non-empty stub directory - manual cleanup needed\n")
            except Exception:
                pass
            return "pool-paused"  # ALERT already logged; fleet start paused
        if have < SW_BINDER_COUNT:
            try:
                with open(os.path.join(st_dir, "boot.degraded"), "w") as fh:
                    fh.write("boot recovery paused: binder pool incomplete (%d/%d) - fleet start paused\n"
                             % (have, SW_BINDER_COUNT))
            except Exception:
                pass
            _sw_log(st_dir, "WARN boot recovery: binder pool incomplete (%d/%d) - fleet start paused, retrying next pass"
                    % (have, SW_BINDER_COUNT))
            return "incomplete"
        # Pool complete: the pause is over (even while the stagger below
        # is still draining), so a marker from an earlier paused pass goes.
        try:
            os.remove(os.path.join(st_dir, "boot.degraded"))
        except Exception:
            pass
        docker_fn = docker_fn or docker
        rc, out = docker_fn("ps", "-a", "--format", "{{.Names}}|{{.State}}", timeout=30)
        if rc != 0:
            _sw_log(st_dir, "WARN boot recovery: docker ps -a failed - retrying next pass")
            return "incomplete"
        running = set()
        for line in (out or "").splitlines():
            line = line.strip()
            if line.endswith("|running"):
                running.add(line[:-len("|running")])
        if instances is None:
            try:
                instances = sorted(load_instances().keys())
            except Exception:
                instances = []
        down = []
        for name in instances:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
                continue
            if os.path.exists(os.path.join(st_dir, name + ".degraded")):
                continue  # a circuit-broken instance never joins the stagger
            if ("wd-" + name) not in running:
                down.append(name)
        down.sort()
        if not down:
            _sw_write_boot_sentinel(sentinel_dir)
            _sw_log(st_dir, "boot recovery complete: fleet already running")
            return "complete"
        started = 0
        failed = 0
        degraded_now = 0
        attempted = 0
        total = len(down)
        for name in down:
            # the budget counts start INVOCATIONS (attempts), not successes:
            # a failed start must not let the next instance jump the queue.
            if attempted >= SW_BOOT_MAX_STARTS:
                continue  # over this pass's budget: stays queued for next pass
            attempted += 1
            n = _sw_note_event(st_dir, name + ".bootstart", 3600)
            if n > SW_BOOT_ATTEMPTS:
                try:
                    with open(os.path.join(st_dir, name + ".degraded"), "w") as fh:
                        fh.write("failed to start %d times during boot recovery - auto-recovery paused\n" % n)
                except Exception:
                    pass
                _sw_log(st_dir, "%s DEGRADED: failed to start %d times during boot recovery - auto-recovery paused (needs human)" % (name, n))
                degraded_now += 1
                continue
            rc2, out2 = docker_fn("start", "wd-" + name, timeout=60)
            if rc2 == 0:
                _sw_log(st_dir, "%s boot recovery: started ok" % name)
                started += 1
                _sw_bump_recoveries(st_dir)
            else:
                _sw_log(st_dir, "%s boot recovery: start FAILED (attempt %d/%d): %s"
                        % (name, n, SW_BOOT_ATTEMPTS, (out2 or "")[-120:]))
                failed += 1
        if started + degraded_now >= total:
            _sw_write_boot_sentinel(sentinel_dir)
            _sw_log(st_dir, "boot recovery complete: started %d, degraded %d, failed-retry %d"
                    % (started, degraded_now, failed))
            return "complete"
        _sw_log(st_dir, "boot recovery: started %d/%d this pass (max %d/pass), %d failed-retry, %d degraded"
                % (started, total, SW_BOOT_MAX_STARTS, failed, degraded_now))
        return "progress"
    except Exception:
        return "error"


def _sw_log(st_dir, msg):
    try:
        os.makedirs(st_dir, exist_ok=True)
        logf = os.path.join(st_dir, "selfwatch.log")
        with open(logf, "a") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%F %T"), msg))
        if os.path.getsize(logf) > 2097152:
            with open(logf) as fh:
                lines = fh.readlines()
            with open(logf, "w") as fh:
                fh.writelines(lines[-300:])
    except Exception:
        pass


def _sw_note_event(st_dir, key, window_s):
    # record one event in a sliding window; return surviving count (incl. this)
    now = int(time.time())
    f = os.path.join(st_dir, key + ".ev")
    keep = []
    try:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line.isdigit() and now - int(line) < window_s:
                    keep.append(line)
    except Exception:
        pass
    keep.append(str(now))
    try:
        with open(f, "w") as fh:
            fh.write("\n".join(keep) + "\n")
    except Exception:
        pass
    return len(keep)


def _sw_cb_guard(st_dir, inst, key, window_s, max_n, what):
    # circuit breaker: returns the event count when the action may proceed,
    # or None when blocked (already degraded, or budget just exhausted and the
    # instance was marked DEGRADED).
    try:
        os.makedirs(st_dir, exist_ok=True)
    except Exception:
        pass
    if os.path.exists(os.path.join(st_dir, inst + ".degraded")):
        return None
    n = _sw_note_event(st_dir, key, window_s)
    if n > max_n:
        try:
            with open(os.path.join(st_dir, inst + ".degraded"), "w") as fh:
                fh.write("%s restarted %d times in last %ss - auto-recovery paused\n"
                         % (what, n, window_s))
        except Exception:
            pass
        _sw_log(st_dir, "%s DEGRADED: %s restarted %d times in last %ss - auto-recovery paused (needs human)"
                % (inst, what, n, window_s))
        return None
    return n


def _sw_bump_recoveries(st_dir):
    f = os.path.join(st_dir, "recoveries.json")
    try:
        with open(f) as fh:
            d = json.load(fh)
    except Exception:
        d = {}
    d["total"] = int(d.get("total", 0)) + 1
    try:
        tmp = f + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, f)
    except Exception:
        pass
    return d["total"]


# --- Waydroid session-death guard (agent 1.0.11) ---
# Remote nodes had no equivalent of the main-node watchdog's v9 session
# check (df-watchdog.sh section 2b): a dead Waydroid session inside a
# running container was invisible to the agent's selfwatch. The container
# stays up (so the container check passes), the booted marker persists (so
# the drift check's booted gate passes), and the helper daemons run through
# `waydroid shell`, so their restarts fail silently — burning the helper
# circuit budget (8/10min) until a misleading "input-daemon.sh restarted 9
# times" DEGRADED, with the root cause never repaired (black stream, dead
# input, Roblox never relaunched). This probes the session directly (same
# getprop idiom as the entrypoint boot check); on death it starts the
# session detached, then the caller returns early so the helper/stream
# checks don't burn their budgets on a known-dead session. Only applies
# once the instance has booted at least once — first boot is not probed.
# Budgeted >4 starts/hour -> instance DEGRADED (reason on the marker's
# first line -> selfwatch_state -> heartbeat -> the dashboard Nodes "needs
# attention" badge). Recovery auto-clears via the existing step-4 path.
# All helpers best-effort, never raise.
SW_SESSION_GRACE_S = 180
SW_SESSION_BUDGET_N = 4
SW_SESSION_BUDGET_WINDOW_S = 3600


def sw_booted_once(name, vol_base="/var/lib/docker/volumes"):
    """True when the instance has booted at least once (booted marker in the
    data volume). Never raises."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
            return False
        base = os.path.join(vol_base, "wd-" + name + "-data", "_data")
        return any(os.path.exists(os.path.join(base, p)) for p in (
            "data/media/0/jerusalem/booted",
            "data/booted",
            "booted",
        ))
    except Exception:
        return False


def sw_session_probe(cname):
    """Probe the Waydroid session: True when it answers boot_completed=1,
    False otherwise (dead session, wedged shell, or failing docker exec —
    the v9 budget + grace keep a flapping probe from storming). The cname
    is built internally from a validated instance name, never user input.
    Never raises."""
    try:
        rc, out = docker("exec", cname, "waydroid", "shell", "getprop",
                         "sys.boot_completed", timeout=15)
        return rc == 0 and (out or "").strip() == "1"
    except Exception:
        return False


def sw_check_session(name, st_dir=None, vol_base="/var/lib/docker/volumes"):
    """One session-death pass for a node instance.

    Returns "alive" (session answering), "started" (death seen, session
    start issued), "grace" (start issued <180s ago, waiting out the Android
    re-boot window), "failed" (death seen, session start failed), "paused"
    (circuit budget exhausted / already DEGRADED — no action), or "skip"
    (invalid name or never booted — first boot is not probed). Logging,
    budget, recovery count, and grace-ts semantics mirror the main-node v9.
    Never raises.
    """
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        return "skip"
    cname = "wd-" + name
    try:
        if not sw_booted_once(name, vol_base):
            return "skip"
    except Exception:
        return "skip"
    try:
        if sw_session_probe(cname):
            # session alive: drop any stale grace timestamp (v9 parity)
            try:
                os.remove(os.path.join(st_dir, name + ".sessionstart.ts"))
            except Exception:
                pass
            return "alive"
        tsf = os.path.join(st_dir, name + ".sessionstart.ts")
        age = None
        try:
            with open(tsf) as fh:
                age = int(time.time()) - int(fh.read().strip().split()[0])
        except Exception:
            age = None
        if age is not None and age < SW_SESSION_GRACE_S:
            _sw_log(st_dir, "%s waydroid session still down (restart issued %ds ago, %ds grace) - waiting"
                    % (name, age, SW_SESSION_GRACE_S))
            return "grace"
        ev = _sw_cb_guard(st_dir, name, name + ".session",
                          SW_SESSION_BUDGET_WINDOW_S, SW_SESSION_BUDGET_N,
                          "waydroid session")
        if ev is None:
            return "paused"
        rc, _ = docker("exec", "-d", cname, "waydroid", "session", "start",
                       timeout=15)
        if rc == 0:
            try:
                with open(tsf, "w") as fh:
                    fh.write(str(int(time.time())) + "\n")
            except Exception:
                pass
            _sw_log(st_dir, "%s waydroid session dead -> session start (%d/%d this hour)"
                    % (name, ev, SW_SESSION_BUDGET_N))
            _sw_bump_recoveries(st_dir)
            return "started"
        _sw_log(st_dir, "%s waydroid session start FAILED" % name)
        return "failed"
    except Exception:
        return "skip"


# agent 1.0.14: stuck-boot detection (main-node df-watchdog.sh section 2
# parity). A wing instance whose Android never finishes booting has its
# container up but no `booted` marker — previously invisible to the agent:
# the drift (1b) and session (1c) checks skip never-booted instances, so
# only the helper checks fired, burning the helper circuit budget until a
# misleading "<script> restarted N times" DEGRADED, with the root cause
# (dead boot) never repaired. Now: container up >15min with no marker ever
# written -> 2 strikes -> docker restart (6/hour circuit, same budget as
# the container check). Complements sw_check_session: that owns the
# booted-once case, this owns first boot.
SW_STUCK_BOOT_S = 900
SW_STUCK_BOOT_STRIKES = 2
SW_STUCK_BOOT_BUDGET_N = 6
SW_STUCK_BOOT_BUDGET_WINDOW_S = 3600

_STARTED_AT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def _sw_parse_started_at(s):
    """docker inspect StartedAt -> epoch seconds, or None. Never raises."""
    try:
        m = _STARTED_AT_RE.match((s or "").strip())
        if not m:
            return None
        y, mo, d, h, mi, sec, frac, tz = m.groups()
        dt = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi),
                               int(sec),
                               int((frac or "0")[:6].ljust(6, "0")))
        if tz is None or tz == "Z":
            tzi = datetime.timezone.utc
        else:
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            tzi = datetime.timezone(sign * datetime.timedelta(
                hours=int(digits[:2]), minutes=int(digits[2:])))
        return dt.replace(tzinfo=tzi).timestamp()
    except Exception:
        return None


def sw_check_stuck_boot(name, st_dir=None, vol_base="/var/lib/docker/volumes",
                        now=None):
    """One stuck-boot pass for a node instance.

    Returns "restarted" (docker restart fired — caller returns early, the
    container is fresh), "stuck" (no booted marker yet — caller marks the
    pass unhealthy but keeps running the other checks, mirroring the
    main-node watchdog; an ambiguous uptime probe also yields "stuck" —
    main sets healthy=0 on a missing marker regardless of probe success —
    but writes no strike and fires no restart), or "skip" (booted marker
    present — strike counter reset — or invalid name). Never raises.
    """
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        return "skip"
    cname = "wd-" + name
    strike_f = os.path.join(st_dir, name + ".stuckboot")
    try:
        if sw_booted_once(name, vol_base):
            # not stuck: drop any strike counter from a previous episode
            try:
                os.remove(strike_f)
            except Exception:
                pass
            return "skip"
    except Exception:
        return "skip"
    if now is None:
        now = time.time()
    try:
        rc, out = docker("inspect", "-f", "{{.State.StartedAt}}", cname,
                         timeout=10)
        start = _sw_parse_started_at(out) if rc == 0 else None
    except Exception:
        start = None
    if start is None or now - start < SW_STUCK_BOOT_S:
        return "stuck"
    # past the 15-minute line with no marker: 2 strikes -> restart
    strikes = 0
    try:
        with open(strike_f) as fh:
            strikes = int(fh.read().strip().split()[0])
    except Exception:
        strikes = 0
    strikes += 1
    try:
        with open(strike_f, "w") as fh:
            fh.write(str(strikes) + "\n")
    except Exception:
        pass
    if strikes < SW_STUCK_BOOT_STRIKES:
        return "stuck"
    ev = _sw_cb_guard(st_dir, name, name + ".stuckboot",
                      SW_STUCK_BOOT_BUDGET_WINDOW_S, SW_STUCK_BOOT_BUDGET_N,
                      "container")
    if ev is None:
        return "stuck"
    try:
        rc, out = docker("restart", cname, timeout=60)
    except Exception:
        rc, out = 1, ""
    if rc == 0:
        _sw_log(st_dir,
                "%s stuck booting (no marker after 15m) -> docker restart (%d/%d this hour)"
                % (name, ev, SW_STUCK_BOOT_BUDGET_N))
        _sw_bump_recoveries(st_dir)
        try:
            os.remove(strike_f)
        except Exception:
            pass
        return "restarted"
    _sw_log(st_dir, "%s stuck-boot restart FAILED: %s"
            % (name, (out or "")[-120:]))
    return "stuck"


# ---- keepalive crash-loop surfacing (agent 1.0.15 — main-node v8/3b parity) ---
# The in-container roblox-keepalive.sh v2 writes keepalive.degraded (reason on
# the first line) when launch attempts exceed budget and backs off to 5-min
# attempts. The main node's df-watchdog mirrors it into the instance .degraded
# marker (check 3b); wing nodes never did, so a crash-looping Roblox on a wing
# node showed a healthy node while the keepalive silently backed off. This is
# the agent-side mirror: same marker convention, same "Roblox keepalive:"
# reason prefix, surfaced through the existing selfwatch_state() degraded map
# -> heartbeat -> dashboard Nodes "needs attention" badge.
SW_KEEPALIVE_DEGRADED = "keepalive.degraded"


def sw_keepalive_marker_path(name, vol_base="/var/lib/docker/volumes"):
    """Host-side path of the in-container keepalive.degraded marker, or None
    for invalid names. The keepalive runs inside the container and writes to
    /data/media/0/jerusalem, which is the shared data volume — the same
    host-side volume layout sw_booted_once reads. Never raises."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
            return None
        return os.path.join(vol_base, "wd-" + name + "-data", "_data",
                            "data", "media", "0", "jerusalem",
                            SW_KEEPALIVE_DEGRADED)
    except Exception:
        return None


def sw_check_keepalive_degraded(name, st_dir=None,
                                vol_base="/var/lib/docker/volumes"):
    """One keepalive-degraded pass for a node instance (agent 1.0.15).

    Returns "degraded" (marker present — the instance counts as unhealthy so
    the step-4 auto-clear waits for the keepalive to recover; it clears its
    own marker when Roblox is focused again), "ok" (no marker), or "skip"
    (invalid name / unreadable — never raises).

    Surfacing only: the mirror is never routed through the circuit-breaker
    budget, a pre-existing <name>.degraded marker is never clobbered, and
    nothing is ever restarted. Nodes whose image predates keepalive v2 never
    write the marker, so the check is a silent no-op there."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    try:
        path = sw_keepalive_marker_path(name, vol_base)
        if path is None:
            return "skip"
        if not os.path.isfile(path):
            return "ok"
        reason = ""
        try:
            with open(path) as fh:
                reason = (fh.readline() or "").strip()
        except Exception:
            reason = ""
        if not reason:
            reason = "launch attempts over budget"
        marker = os.path.join(st_dir, name + ".degraded")
        if not os.path.exists(marker):
            try:
                with open(marker, "w") as fh:
                    fh.write("Roblox keepalive: %s\n" % reason[:160])
            except Exception:
                pass
            else:
                _sw_log(st_dir, "%s DEGRADED: Roblox keepalive backed off (%s) - needs human"
                        % (name, reason[:120]))
        return "degraded"
    except Exception:
        return "skip"


# --- Roblox install-failure surfacing (agent 1.0.35 — main-node watchdog
# v15 check 3c parity) ---
# The same waydroid-instance image runs on wing nodes, so its
# boot-complete.sh writes roblox_install_failed (reason on first line) into
# the data volume when the one-shot pm install fails, times out, or the APKs
# are missing from the image. The main node mirrors it in df-watchdog.sh
# v15 check 3c; wing agents never did, so a failed install on a node
# instance sat on "installing_roblox" forever with no "needs attention"
# badge. The seventeenth "main had it, wing didn't" parity item (1.0.5
# drift, 1.0.6 adaptive-screencap, 1.0.7 disk/log, 1.0.11 session-death,
# 1.0.12 loop devices, 1.0.13 memory, 1.0.14 stuck-boot, 1.0.15 keepalive
# mirror, 1.0.16 binderfs self-heal, 1.0.17 agent HTTP self-check, 1.0.18
# create image preflight, 1.0.19 auth rate-limit, 1.0.20 status cache,
# 1.0.21 headroom probe, 1.0.22 per-instance degraded, 1.0.23 selfwatch
# lock, 1.0.24 self-update, 1.0.25 lock race, 1.0.26 route-name-gate,
# 1.0.27 caps, 1.0.28 ratelimit-tunnel, 1.0.29 missing-script guard,
# 1.0.30 update rollback, 1.0.31 instance-resize, 1.0.32 boot recovery,
# 1.0.33 binder-safe-start, 1.0.34 start-route dedup).
SW_INSTALL_FAILED_MARKER = "roblox_install_failed"


def sw_install_failed_marker_path(name, vol_base="/var/lib/docker/volumes"):
    """Host-side path of the in-container roblox_install_failed marker, or
    None for invalid names. Same data-volume layout as
    sw_keepalive_marker_path. Never raises."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
            return None
        return os.path.join(vol_base, "wd-" + name + "-data", "_data",
                            "data", "media", "0", "jerusalem",
                            SW_INSTALL_FAILED_MARKER)
    except Exception:
        return None


def sw_check_install_failed(name, st_dir=None,
                            vol_base="/var/lib/docker/volumes"):
    """One install-failure pass for a node instance (agent 1.0.35).

    Returns "degraded" (marker present — the instance counts as unhealthy so
    the step-4 auto-clear waits for boot-complete to clear its own marker on
    a later successful run), "ok" (no marker), or "skip" (invalid name /
    unreadable — never raises).

    Surfacing only: never routed through the circuit-breaker budget, a
    pre-existing <name>.degraded marker is never clobbered, nothing is ever
    restarted. Nodes whose image predates the install-guard boot-complete.sh
    never write the marker, so the check is a silent no-op there. The
    "Roblox install failed:" reason prefix matches df-watchdog.sh v15 for
    cross-node consistency in the dashboard Eventos modal."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    try:
        path = sw_install_failed_marker_path(name, vol_base)
        if path is None:
            return "skip"
        if not os.path.isfile(path):
            return "ok"
        reason = ""
        try:
            with open(path) as fh:
                reason = (fh.readline() or "").strip()
        except Exception:
            reason = ""
        if not reason:
            reason = "unknown error"
        marker = os.path.join(st_dir, name + ".degraded")
        if not os.path.exists(marker):
            try:
                with open(marker, "w") as fh:
                    fh.write("Roblox install failed: %s\n" % reason[:160])
            except Exception:
                pass
            else:
                _sw_log(st_dir, "%s DEGRADED: Roblox install failed (%s) - needs human"
                        % (name, reason[:120]))
        return "degraded"
    except Exception:
        return "skip"


# --- Agent HTTP self-check (agent 1.0.17) ---
# Remote nodes had no equivalent of the main-node watchdog's manager
# self-check (df-watchdog.sh v5, probe hardened in v12): a wedged-but-alive
# agent HTTP server (thread pool exhausted — the exact failure
# http-server-bounds mitigates but cannot repair) is invisible to systemd
# and stops heartbeats, screenshots, creates and logins while the process
# looks alive. Once per selfwatch pass the agent probes its own
# /api/agent/health and requires a COMPLETE body (v12 parity): a 2xx with a
# truncated body counts as a failed probe with a distinct log line.
# 2 consecutive failures -> one ALERT log line and process exit; systemd
# Restart=always (install-wing.sh) revives the agent in ~5s. Circuit-broken:
# >2 self-exits/hour -> agent.http.degraded marker (reason on the first
# line), alert-only until a healthy probe clears it. Guarded: skipped while
# the server is not bound yet (_AGENT_HTTP_BOUND, set by main() after the
# bind), and skipped when not running under systemd (INVOCATION_ID /
# JOURNAL_STREAM absent) — exiting there would orphan the node with no
# supervisor to revive it; one WARN per day, never silent. The degraded
# marker flows through selfwatch_state() -> heartbeat -> the dashboard
# Nodes "needs attention" badge; the ALERT / DEGRADED / recovered lines go
# to selfwatch.log -> the dashboard Eventos modal. Zero manager/dashboard
# changes. All helpers best-effort, never raise.
SW_AGENT_HTTP_TIMEOUT = 10
SW_AGENT_HTTP_STRIKES = 2
SW_AGENT_HTTP_MAX_EXITS = 2
SW_AGENT_HTTP_WINDOW = 3600
_AGENT_HTTP_BOUND = False


def sw_under_systemd(env=None):
    """True when the process runs under a systemd service (which will
    restart it after an exit). env injectable for tests. Never raises."""
    try:
        if env is None:
            env = os.environ
        return bool(env.get("INVOCATION_ID") or env.get("JOURNAL_STREAM"))
    except Exception:
        return False


def sw_probe_agent_http(port, path="/api/agent/health", timeout=SW_AGENT_HTTP_TIMEOUT):
    """Probe the agent's own HTTP server. Returns (status, truncated):
    ("ok", False) on a complete 2xx/3xx with a non-empty body;
    ("bad", True) when 2xx/3xx arrived but the body did not complete
    (v12 parity); ("bad", False) otherwise. Never raises."""
    # agent-sw-numerics-sink-gate: coerce + range-check the port and the
    # timeout at the sink, before any network attempt. Invalid values
    # fail closed with zero urlopen calls (old code: int(port) only, so
    # 0/99999/True sailed into a URL build + probe attempt, and a bad
    # timeout went straight to urlopen).
    port = _sw_coerce_probe_port(port)
    timeout = _sw_coerce_probe_timeout(timeout)
    if port is None or timeout is None:
        return ("bad", False)
    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        req = urllib.request.Request(url, headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            try:
                body = resp.read()
            except Exception:
                return ("bad", True)
            if not body:
                return ("bad", True)
            if 200 <= code < 400:
                return ("ok", False)
            return ("bad", False)
    except Exception:
        return ("bad", False)


def sw_check_agent_http(port=None, st_dir=None, env=None, probe=None,
                        exit_fn=None):
    """One agent-HTTP self-check pass. Returns "ok" | "strike" | "exited" |
    "degraded" | "skipped". On the second consecutive failed probe the
    process exits (exit_fn, default os._exit(1)) so systemd revives it;
    the exit budget (>2/hour) degrades to alert-only. Never raises."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    if exit_fn is None:
        def exit_fn():
            os._exit(1)
    if probe is None:
        probe = sw_probe_agent_http
    try:
        if IS_WINDOWS:
            return "skipped"
        if not _AGENT_HTTP_BOUND:
            return "skipped"  # server not bound yet; silent (startup race)
        if not sw_under_systemd(env):
            # No supervisor would restart the process: exiting would orphan
            # the node. One WARN per day, never silent.
            dayf = os.path.join(st_dir,
                                "agent.http.nosystemd." + time.strftime("%F"))
            try:
                if not os.path.exists(dayf):
                    for fn in os.listdir(st_dir):
                        if fn.startswith("agent.http.nosystemd."):
                            try:
                                os.remove(os.path.join(st_dir, fn))
                            except Exception:
                                pass
                    open(dayf, "w").close()
                    _sw_log(st_dir, "WARN agent HTTP self-check disabled: not "
                            "running under systemd (no supervisor would "
                            "restart the process)")
            except Exception:
                pass
            return "skipped"
        if port is None:
            port = BOUND_PORT
        marker = os.path.join(st_dir, "agent.http.degraded")
        sfile = os.path.join(st_dir, "agent.http.strikes")
        status, truncated = probe(port)
        if status == "ok":
            try:
                os.remove(sfile)
            except Exception:
                pass
            if os.path.exists(marker):
                for fn in ("agent.http.degraded", "agent.http.exit.ev"):
                    try:
                        os.remove(os.path.join(st_dir, fn))
                    except Exception:
                        pass
                _sw_log(st_dir, "agent HTTP server recovered from DEGRADED "
                        "(healthy probe) - self-check resumed")
            return "ok"
        n = 0
        try:
            n = int((open(sfile).read().strip() or "0"))
        except Exception:
            n = 0
        n += 1
        try:
            with open(sfile, "w") as fh:
                fh.write(str(n))
        except Exception:
            pass
        if os.path.exists(marker):
            # Already degraded: alert-only, never exit. One line per probe
            # keeps the failure visible in the Eventos modal without firing
            # a new DEGRADED alert.
            _sw_log(st_dir, "agent HTTP probe failed "
                    "(already degraded, alert-only)")
            return "degraded"
        if truncated:
            _sw_log(st_dir, "agent HTTP 200 but body truncated "
                    "(strike %d/%d)" % (n, SW_AGENT_HTTP_STRIKES))
        else:
            _sw_log(st_dir, "agent HTTP probe failed (strike %d/%d)"
                    % (n, SW_AGENT_HTTP_STRIKES))
        if n < SW_AGENT_HTTP_STRIKES:
            return "strike"
        ev = _sw_cb_guard(st_dir, "agent.http", "agent.http.exit",
                          SW_AGENT_HTTP_WINDOW, SW_AGENT_HTTP_MAX_EXITS,
                          "agent HTTP server")
        if ev is None:
            return "degraded"  # budget exhausted or already degraded
        _sw_log(st_dir, "ALERT agent HTTP server unresponsive twice - "
                "exiting for systemd restart (exit %d/%d this hour)"
                % (ev, SW_AGENT_HTTP_MAX_EXITS))
        _sw_bump_recoveries(st_dir)
        try:
            os.remove(sfile)
        except Exception:
            pass
        exit_fn()
        return "exited"
    except Exception:
        return "skipped"


# --- Missing helper-script guard (agent 1.0.29 — main-node boot-complete parity) ---
# The canonical boot sequence (boot-complete.sh, run by the entrypoint fast
# path and by booted-watcher.sh on a late boot) installs the helper scripts
# into the instance's shared jerusalem/ dir. A slow first boot on an image
# predating boot-complete.sh finishes booting WITHOUT ever installing them:
# pgrep sees no helpers, so the restart below runs
# `waydroid shell sh <missing-script>`, which fails silently inside Android
# (docker exec -d returns rc 0 because the detached sh launched fine). The
# result is a lying "dead -> restart" log line, a bumped recovery count, and
# the 8/10min helper circuit budget burning toward a misleading
# "input-daemon.sh restarted 9 times" DEGRADED — the exact silent failure the
# main watchdog fixed in its sections 3+4 (boot-complete, 2026-10-20).
# When a script is missing from the data volume: one WARN per day naming the
# real cause, skip the restart, and do NOT burn the circuit budget.
# Surfacing only — never raises, never deletes anything.
def sw_helper_script_path(name, script, vol_base="/var/lib/docker/volumes"):
    # Host-side path of a helper script inside the instance's data volume
    # (mirrors the volume layout check 3 uses for live.png).
    return os.path.join(vol_base, "wd-" + name + "-data", "_data",
                        "data", "media", "0", "jerusalem", script)


def sw_warn_helper_missing(st_dir, name, script, vol_base="/var/lib/docker/volumes"):
    # True when the helper script is missing from the data volume. Logs one
    # WARN per day (dated marker file; stale markers cleaned) and returns
    # True so the caller skips the restart without touching the
    # circuit-breaker budget. Never raises.
    try:
        if os.path.isfile(sw_helper_script_path(name, script, vol_base)):
            return False
        day = time.strftime("%F")
        safe = re.sub(r"[^a-zA-Z0-9]", "_", script or "")
        marker = os.path.join(st_dir, "%s.helper-missing-%s.%s" % (name, safe, day))
        if not os.path.exists(marker):
            prefix = "%s.helper-missing-%s." % (name, safe)
            try:
                for fn in os.listdir(st_dir):
                    if fn.startswith(prefix) and fn != os.path.basename(marker):
                        try:
                            os.remove(os.path.join(st_dir, fn))
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                open(marker, "w").close()
            except Exception:
                pass
            _sw_log(st_dir, "%s WARN: %s missing from data volume - boot setup "
                            "never installed it; cannot restart "
                            "(recreate the instance from a current image)" % (name, script))
        return True
    except Exception:
        return False


def selfwatch_check_instance(name, st_dir=None, vol_base="/var/lib/docker/volumes",
                             boot_recovery=None):
    """One recovery pass for a node instance. Returns True when all green.

    st_dir / vol_base are injectable for tests; production defaults apply.
    boot_recovery is the status string from sw_boot_recovery() when the
    caller already ran section 0 this pass (selfwatch_pass_once passes it);
    None means section 0 was not run — the old behavior (no deferral) is
    preserved so direct callers and hermetic tests are unaffected.
    """
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        return True
    cname = "wd-" + name
    healthy = True
    # 0. adaptive-screencap marker sweep (agent 1.0.6, alert-only): nobody
    # fetched a screenshot recently -> drop stream_wanted so the
    # in-container loop idles at 2s. Never affects `healthy`, never raises.
    try:
        _sweep_stream_want(name, vol_base)
    except Exception:
        pass
    # 0b. stale b64 input staging sweep (remote-b64-sweep, alert-only):
    # a staged secret older than 2x the cleanup TTL is orphaned — its
    # thread died with a past agent process or never started — so remove
    # it. Never affects `healthy`, never raises; a live stage is at most
    # 60s old and its FIFO eval already happened, so the age gate cannot
    # break a live typing.
    try:
        n_b64 = sweep_stale_b64_input_text(names=[name], vol_base=vol_base,
                                           max_age_s=2 * _B64_TEXT_TTL_S)
        if n_b64:
            _sw_log(st_dir, "%s stale b64 input staging removed (%d file(s))"
                    % (name, n_b64))
    except Exception:
        pass
    # 1. container running? (2 strikes -> docker start; circuit-broken 6/hour)
    rc, out = docker("ps", "--format", "{{.Names}}", timeout=10)
    up = rc == 0 and cname in (out or "").split()
    if not up:
        sf = os.path.join(st_dir, name + ".norun")
        n = 0
        try:
            n = int((open(sf).read().strip() or "0"))
        except Exception:
            n = 0
        n += 1
        try:
            with open(sf, "w") as fh:
                fh.write(str(n))
        except Exception:
            pass
        if n >= 2:
            # (agent 1.0.32) while boot recovery owns the staggered starts,
            # starting here would race the stagger and recreate the herd:
            # count the strike, leave the start to section 0.
            recovering = (boot_recovery in ("progress", "incomplete", "pool-paused")
                          if boot_recovery is not None else False)
            if recovering:
                _sw_log(st_dir, "%s container down (strike %d) - boot recovery active, start deferred" % (name, n))
            else:
                ev = _sw_cb_guard(st_dir, name, name + ".restart", 3600, 6, "container")
                if ev is not None:
                    rc2, out2 = docker("start", cname, timeout=60)
                    if rc2 == 0:
                        _sw_log(st_dir, "%s container down -> docker start (restart %d/6 this hour)"
                                % (name, ev))
                        _sw_bump_recoveries(st_dir)
                    else:
                        _sw_log(st_dir, "%s docker start FAILED: %s" % (name, (out2 or "")[-120:]))
            try:
                os.remove(sf)
            except Exception:
                pass
        return False
    try:
        os.remove(os.path.join(st_dir, name + ".norun"))
    except Exception:
        pass
    # 1b. Roblox version-drift surfacing (agent 1.0.5). Alert-only: it never
    # flips `healthy` and never touches the recovery budgets — a drifted
    # client needs a human decision, not an auto-restart.
    try:
        check_roblox_drift(name, st_dir=st_dir, vol_base=vol_base)
    except Exception:
        pass
    # 1c. waydroid session alive? (agent 1.0.11 — main-node v9 parity). A
    #     dead session is invisible to every check below: the container
    #     stays up, the booted marker persists, and the helpers run through
    #     `waydroid shell`, so their restarts fail silently while burning
    #     the helper circuit budget. On death the session is started
    #     detached and we return early so the helper/stream checks don't
    #     burn their budgets on a known-dead session.
    try:
        sess = sw_check_session(name, st_dir=st_dir, vol_base=vol_base)
    except Exception:
        sess = "skip"
    if sess != "alive" and sess != "skip":
        return False
    # 1d. stuck boot? (agent 1.0.14 — main-node watchdog section 2 parity).
    #     Never-booted instance whose container has been up >15min ->
    #     docker restart (2 strikes, 6/hour circuit). Complements 1c: that
    #     owns the booted-once case, this owns first boot. Restart -> early
    #     return (the container is fresh); still-stuck -> unhealthy, but the
    #     helper/stream checks still run (main parity).
    try:
        sb = sw_check_stuck_boot(name, st_dir=st_dir, vol_base=vol_base)
    except Exception:
        sb = "skip"
    if sb == "restarted":
        return False
    if sb == "stuck":
        healthy = False
    # 2. helper daemons alive? (screencap loop, input daemon, keepalive)
    loop_restarted = False  # set when check 2 restarts screencap-loop.sh
    # (check 3's missing-frame branch reads it so one pass never restarts
    # the loop twice)
    for script in _SELF_WATCH_HELPERS:
        rc, _ = docker("exec", cname, "pgrep", "-f", script, timeout=15)
        if rc != 0:
            healthy = False
            if sw_warn_helper_missing(st_dir, name, script, vol_base):
                # (agent 1.0.29) script never installed into the data volume:
                # restarting it would fail silently, so say so plainly and
                # do NOT burn the circuit-breaker budget on a certain failure.
                continue
            key = name + ".helper-" + re.sub(r"[^a-zA-Z0-9]", "_", script)
            ev = _sw_cb_guard(st_dir, name, key, 600, 8, script)
            if ev is not None:
                rc2, out2 = docker("exec", "-d", cname, "sh", "-c",
                                   "nohup waydroid shell sh %s/%s >/dev/null 2>&1"
                                   % (_SELF_WATCH_DF, script), timeout=15)
                if rc2 == 0:
                    _sw_log(st_dir, "%s %s dead -> restart (%d/8 per 10m)" % (name, script, ev))
                    _sw_bump_recoveries(st_dir)
                    if script == "screencap-loop.sh":
                        loop_restarted = True
                else:
                    _sw_log(st_dir, "%s %s restart FAILED: %s" % (name, script, (out2 or "")[-120:]))
    # 3. screencap fresh? (stale >30s -> kill + restart loop; cb 10/hour)
    # Missing live.png (age == -1) is the same failure family, handled by
    # the elif below (missing-frame-guard): the old code only handled a
    # STALE file, so a loop that never produced a frame was never
    # repaired on remote nodes either.
    png = os.path.join(vol_base, cname + "-data", "_data",
                       "data", "media", "0", "jerusalem", "live.png")
    try:
        age = time.time() - os.path.getmtime(png)
    except Exception:
        age = -1
    if age >= 30:
        healthy = False
        if sw_warn_helper_missing(st_dir, name, "screencap-loop.sh", vol_base):
            # (agent 1.0.29) same missing-script case as check 2: restarting
            # the loop would fail silently. Distinct WARN, no pkill, no
            # circuit-budget burn.
            pass
        else:
            ev = _sw_cb_guard(st_dir, name, name + ".staleloop", 3600, 10, "screencap-loop")
            if ev is not None:
                docker("exec", cname, "pkill", "-f", "screencap-loop.sh", timeout=15)
                rc2, _ = docker("exec", "-d", cname, "sh", "-c",
                                "nohup waydroid shell sh %s/screencap-loop.sh >/dev/null 2>&1"
                                % _SELF_WATCH_DF, timeout=15)
                if rc2 == 0:
                    _sw_log(st_dir, "%s screencap stale (%ds) -> restart loop (%d/10 per hour)"
                            % (name, int(age), ev))
                    _sw_bump_recoveries(st_dir)
    elif age == -1:
        # Missing-frame guard (agent 1.0.42 — main-node watchdog section 4b
        # parity): live.png never written while the loop is alive means
        # screencap fails every cycle, so the viewer spins on reconnects
        # with no repair and no DEGRADED badge. The loop installs live.png
        # by atomic mv, so a missing file is never a mid-write transient.
        # Booted gate: a first-boot instance legitimately has no frame yet.
        # A dead loop is check 2's job (and a loop restarted by check 2
        # this pass is skipped via loop_restarted, so one pass never
        # restarts twice). Shares the staleloop budget with the stale
        # branch. Never raises: every helper here is fail-closed.
        healthy = False
        try:
            booted = sw_booted_once(name, vol_base)
        except Exception:
            booted = False
        if not booted:
            pass  # first boot: no frame yet is normal, not a failure
        elif sw_warn_helper_missing(st_dir, name, "screencap-loop.sh", vol_base):
            pass  # WARN already logged, no budget burn (same as stale branch)
        elif loop_restarted:
            pass  # check 2 restarted the loop this pass; next pass judges it
        else:
            rc_pg, _ = docker("exec", cname, "pgrep", "-f", "screencap-loop.sh", timeout=15)
            if rc_pg != 0:
                pass  # loop dead: check 2 owns the restart
            else:
                ev = _sw_cb_guard(st_dir, name, name + ".staleloop", 3600, 10, "screencap-loop")
                if ev is not None:
                    docker("exec", cname, "pkill", "-f", "screencap-loop.sh", timeout=15)
                    rc2, _ = docker("exec", "-d", cname, "sh", "-c",
                                    "nohup waydroid shell sh %s/screencap-loop.sh >/dev/null 2>&1"
                                    % _SELF_WATCH_DF, timeout=15)
                    if rc2 == 0:
                        _sw_log(st_dir, "%s screencap missing (no live.png) -> restart loop (%d/10 per hour)"
                                % (name, ev))
                        _sw_bump_recoveries(st_dir)
    # 3b. Roblox keepalive crash-loop surfacing (agent 1.0.15 — main-node
    #     watchdog check 3b parity). The in-container keepalive v2 writes
    #     keepalive.degraded when launch attempts exceed budget; mirror it
    #     as the instance .degraded marker so the dashboard Nodes
    #     "needs attention" badge shows it. Surfacing only — never routed
    #     through cb_guard, never clobbers a pre-existing marker. While
    #     present the instance counts as unhealthy, so the step-4 auto-clear
    #     waits for the keepalive to recover (it clears its own marker when
    #     Roblox is focused again).
    try:
        kd = sw_check_keepalive_degraded(name, st_dir=st_dir, vol_base=vol_base)
    except Exception:
        kd = "skip"
    if kd == "degraded":
        healthy = False
    # 3c. Roblox install-failure surfacing (agent 1.0.35 — main-node
    #     watchdog v15 check 3c parity). boot-complete.sh writes
    #     roblox_install_failed into the data volume when the one-shot pm
    #     install fails, times out, or the APKs are missing from the image;
    #     mirror it as the instance .degraded marker so the dashboard Nodes
    #     "needs attention" badge shows it. Surfacing only — never routed
    #     through cb_guard, never clobbers a pre-existing marker. While
    #     present the instance counts as unhealthy, so the step-4 auto-clear
    #     waits for boot-complete to clear its own marker on a later
    #     successful run.
    try:
        im = sw_check_install_failed(name, st_dir=st_dir, vol_base=vol_base)
    except Exception:
        im = "skip"
    if im == "degraded":
        healthy = False
    # 4. degraded auto-clear: all checks green -> resume auto-recovery
    marker = os.path.join(st_dir, name + ".degraded")
    if healthy and os.path.exists(marker):
        for fn in os.listdir(st_dir):
            if fn.startswith(name + ".") and (fn.endswith(".ev") or fn.endswith(".degraded")):
                try:
                    os.remove(os.path.join(st_dir, fn))
                except Exception:
                    pass
        # v9 parity: drop the session-guard grace ts on full recovery too
        # (the alive branch already drops it on every healthy probe; this
        # covers the never-booted edge).
        try:
            os.remove(os.path.join(st_dir, name + ".sessionstart.ts"))
        except Exception:
            pass
        _sw_log(st_dir, "%s recovered from DEGRADED (all checks green) - auto-recovery resumed" % name)
    return healthy


def selfwatch_state(st_dir=None):
    """Summary for heartbeats / health endpoint: recovery count + degraded map."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    total = 0
    try:
        with open(os.path.join(st_dir, "recoveries.json")) as fh:
            total = int(json.load(fh).get("total", 0))
    except Exception:
        pass
    degraded = {}
    try:
        for fn in os.listdir(st_dir):
            if fn.endswith(".degraded"):
                try:
                    with open(os.path.join(st_dir, fn)) as fh:
                        reason = fh.readline().strip()
                except Exception:
                    reason = ""
                degraded[fn[:-len(".degraded")]] = reason[:160]
    except Exception:
        pass
    return {"recoveries": total, "degraded": degraded}


# Farm-wide selfwatch files (main-node df-watchdog.sh parity): a
# same-named instance must never sweep them — an instance literally
# called "disk" shares its .degraded filename with the global disk
# marker, and the two are indistinguishable on disk.
_SELFWATCH_GLOBAL_FILES = {
    "disk.degraded", "loop.degraded", "mem.degraded", "agent.http.degraded",
    "boot.degraded",
    "disk.prune.ev", "mem.low.ev", "mem.oom.ev", "log.trim.ev",
    "loop.ensure.ev", "docker.restart.ev", "agent.http.exit.ev",
}


def drop_selfwatch_state(name, st_dir=None):
    """Remove per-instance selfwatch state for a deleted instance
    (delete-state-sweep, agent side of the manager's delete sweep).
    The agent's selfwatch discovers instances from instances.json, so a
    deleted instance's markers were never cleaned: <name>.degraded /
    .roblox_drift / .roblox_drift_seen / .sessionstart.ts and the
    .ev circuit-breaker windows. A recreated instance with the same
    name inherited the dead instance's "needs attention" badge in the
    Nodes modal until the selfwatch auto-clear saw all checks green.
    recoveries.json is a node-wide total, not per-instance, so it is
    left alone. Returns the number of files removed. Never raises."""
    try:
        if not valid_agent_instance_name(name):
            return 0
        if st_dir is None:
            st_dir = SELF_WATCH_DIR
        removed = 0

        def _drop(fn):
            nonlocal removed
            if fn in _SELFWATCH_GLOBAL_FILES:
                return
            p = os.path.join(st_dir, fn)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed += 1
            except Exception:
                pass

        for suffix in (".degraded", ".roblox_drift", ".roblox_drift_seen",
                       ".sessionstart.ts"):
            _drop(name + suffix)
        try:
            names = os.listdir(st_dir)
        except Exception:
            return removed
        for fn in names:
            # Exact-name prefix: "rbx1." never matches "rbx10.degraded".
            if fn.startswith(name + ".") and fn.endswith(".ev"):
                _drop(fn)
        return removed
    except Exception:
        return 0


# --- node selfwatch single-instance lock (agent 1.0.23) ----------------------
# df-watchdog.sh v11 parity: the agent binds 8090-8099 with a port-conflict
# fallback, so a stale or duplicate agent process on the same node (crashed
# installer re-run, manual start, the __main__ restart loop racing systemd)
# binds the next free port and BOTH processes run the full selfwatch recovery
# loop — duplicate container starts, double helper restarts, double disk
# prunes — while heartbeating the same provider ID on different ports. The
# main-node watchdog fixed the same class with a mkdir mutex; this is the
# wing-side equivalent: exactly one recovery actor per node.
#
# The lock is a mkdir mutex at <SELF_WATCH_DIR>/selfwatch.lock holding the
# holder's pid. The holder refreshes the lock-dir mtime every pass
# (SELF_WATCH_INTERVAL = 60s); a lock older than SW_LOCK_STALE_SECS (180s)
# or with a dead holder pid is taken over. mkdir is atomic, so two racers
# always resolve to one holder. The non-holder ("foreign") process keeps
# serving its HTTP API and heartbeating — only the shared-state recovery
# actions are skipped (one WARN, then silent). The per-process HTTP
# self-check (1.0.17) stays lock-independent: a wedged listener must still
# exit its own process so systemd can revive it. A lock dir that cannot be
# created or inspected at all ("broken") fails safe to skip + one WARN per
# day (v11 semantics — never double-recover blindly). Takeover of a live
# holder is possible only inside the two-syscall rmdir+mkdir window while
# its mtime cannot be read; the cost is at most one duplicated pass, never
# a storm. All helpers best-effort, never raise.
SW_LOCK_STALE_SECS = 180
# Grace for the mkdir winner to write its pidfile: the winner writes it in
# the two syscalls right after mkdir, so a missing pidfile on first read
# usually means we lost the race by microseconds, not that the holder died.
# Without the grace, the loser deletes the winner's lock dir before the
# pidfile lands and the winner reports "broken" (false signal + WARN).
# A crashed winner never writes, so the takeover below still recovers it.
SW_LOCK_PIDFILE_GRACE_S = 0.5
_SW_LOCK_FOREIGN_NOTED = False
_SW_LOCK_BROKEN_DAY = ""


def _sw_pid_alive(pid):
    """True when pid names a live process. Never raises."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but not ours
    except Exception:
        return False
    return True


def _sw_selfwatch_lock_inner(st_dir):
    lockdir = os.path.join(st_dir, "selfwatch.lock")
    pidfile = os.path.join(lockdir, "pid")
    me = str(os.getpid())
    pending_takeover = None
    for _ in range(3):
        try:
            os.makedirs(st_dir, exist_ok=True)
        except Exception as e:
            return ("broken", {"st_dir": st_dir,
                               "reason": "state dir: %s" % e})
        # 1. create attempt (atomic: exactly one racer wins)
        try:
            os.mkdir(lockdir)
            created = True
        except FileExistsError:
            created = False
        except Exception as e:
            return ("broken", {"st_dir": st_dir,
                               "reason": "mkdir failed: %s" % e})
        if created:
            try:
                with open(pidfile, "w") as fh:
                    fh.write(me)
            except Exception as e:
                return ("broken", {"st_dir": st_dir,
                                   "reason": "pid write failed: %s" % e})
            return ("held", {"st_dir": st_dir, "pid": me,
                             "takeover": pending_takeover})
        # 2. lock exists: inspect the holder
        try:
            with open(pidfile) as fh:
                holder = fh.read().strip() or None
        except Exception:
            holder = None
        if holder is None:
            # Pidfile grace (see SW_LOCK_PIDFILE_GRACE_S): the mkdir winner
            # may not have written its pidfile yet. Wait a beat before
            # treating the lock as abandoned.
            deadline = time.time() + SW_LOCK_PIDFILE_GRACE_S
            while holder is None and time.time() < deadline:
                time.sleep(0.05)
                try:
                    with open(pidfile) as fh:
                        holder = fh.read().strip() or None
                except Exception:
                    pass
        if holder == me:
            # Re-entrant: we already hold it (e.g. the lock outlived a
            # supervisor thread restart). Refresh and carry on.
            try:
                os.utime(lockdir, None)
            except Exception:
                pass
            return ("held", {"st_dir": st_dir, "pid": me,
                             "takeover": None})
        try:
            age = time.time() - os.path.getmtime(lockdir)
        except Exception:
            age = None
        if _sw_pid_alive(holder) and age is not None \
                and age < SW_LOCK_STALE_SECS:
            return ("foreign", {"st_dir": st_dir, "pid": holder})
        # 3. dead or stale holder: take over, then loop back to the atomic
        # create (a lost race lands in the "foreign" branch above).
        if not _sw_pid_alive(holder):
            pending_takeover = ("previous holder pid %s is dead"
                                % (holder or "?"))
        else:
            pending_takeover = ("previous holder pid %s went stale (%.0fs)"
                                % (holder, age if age is not None else -1))
        try:
            os.remove(pidfile)
        except Exception:
            pass
        try:
            os.rmdir(lockdir)
        except Exception:
            pass
    return ("broken", {"st_dir": st_dir,
                       "reason": "lock contention did not settle"})


def sw_selfwatch_lock(st_dir=None):
    """Arbitrate the selfwatch recovery loop across agent processes on this
    node. Returns (state, detail): "held" (we own the lock — run recovery),
    "foreign" (another live agent holds a fresh lock — skip recovery),
    "broken" (lock unusable — skip recovery). detail carries st_dir, the
    holder pid, a human-readable reason, and the takeover cause when this
    call took the lock over. Never raises."""
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    try:
        return _sw_selfwatch_lock_inner(st_dir)
    except Exception as e:
        return ("broken", {"st_dir": st_dir, "reason": "unexpected: %s" % e})


def _sw_lock_note_foreign(st_dir, pid):
    global _SW_LOCK_FOREIGN_NOTED
    if _SW_LOCK_FOREIGN_NOTED:
        return
    _SW_LOCK_FOREIGN_NOTED = True
    _sw_log(st_dir, "WARN another agent process holds the selfwatch lock "
                    "(pid %s) - recovery actions paused on this process" % pid)


def _sw_lock_note_takeover(st_dir, why):
    global _SW_LOCK_FOREIGN_NOTED
    _SW_LOCK_FOREIGN_NOTED = False
    _sw_log(st_dir, "selfwatch lock taken over (%s) - recovery resumed" % why)


def _sw_lock_note_broken(st_dir, reason, today=None):
    global _SW_LOCK_BROKEN_DAY
    if today is None:
        try:
            today = time.strftime("%F")
        except Exception:
            today = ""
    if today and _SW_LOCK_BROKEN_DAY == today:
        return
    _SW_LOCK_BROKEN_DAY = today
    _sw_log(st_dir, "WARN selfwatch lock unusable (%s) - recovery paused"
                    % reason)


# --- node docker-daemon guard (agent 1.0.39) --------------------------------
# Main-node df-watchdog.sh v15 section 0b parity: the agent's selfwatch
# covered containers, helpers, streams, disk, memory, and the agent HTTP
# listener — but NOT the docker daemon itself. A down daemon looked exactly
# like dead containers: `docker ps` fails fast (daemon connection refused,
# not a timeout), so selfwatch_check_instance section 1 fired for EVERY
# instance — 2 strikes -> `docker start`, which also fails — and within the
# hour the 6/hour circuit breaker marked EVERY instance DEGRADED and paused
# auto-recovery node-wide. The root cause (dead dockerd) was never repaired,
# the markers lied about the cause, and after a human restarted dockerd
# nothing recovered cleanly (section-4 auto-clear needs all-green checks,
# which down containers never give). This guard runs first in every
# lock-held selfwatch pass; while the probe fails, boot recovery, the
# per-instance loop, and the disk auto-prune are skipped (no strikes burned,
# no budgets tripped, no misleading markers; one log line per pass). After
# 2 consecutive failures it restarts dockerd via `systemctl restart docker`
# (2/hour cb_guard budget, same shape as the agent HTTP self-check); beyond
# budget -> global docker.degraded marker (reason on first line ->
# selfwatch_state() degraded map -> heartbeat -> dashboard Nodes "needs
# attention" badge, alert-only), auto-clears on a healthy probe. A dockerd
# restart is recoverable by design: `docker run` sets no --restart policy,
# so stopped containers come back through the normal section-1 start path
# once the probe passes — with clean budgets, because the outage burned
# none. Disk/memory/loop/log guards are docker-independent and keep running
# during an outage (the disk alert WARN still fires; only the prune
# attempt is skipped, so a dead daemon cannot burn the disk budget either).
SW_DOCKER_DAEMON_STRIKES = 2   # consecutive failed probes -> restart attempt
SW_DOCKER_RESTART_BUDGET = 2   # dockerd restarts/hour before docker.degraded
def sw_check_docker_daemon(st_dir=None):
    """Probe the docker daemon; gate all docker-dependent recovery on it.

    Returns True when the daemon answers (`docker info` rc 0), False while
    it does not. A hung probe (docker() returns 124 on timeout) counts as
    down, not unknown — instance recovery cannot run against a daemon that
    will not answer. Best-effort, never raises.
    """
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    try:
        os.makedirs(st_dir, exist_ok=True)
    except Exception:
        pass
    try:
        rc, _ = docker("info", timeout=15)
    except Exception:
        rc = 1
    if rc == 0:
        try:
            os.remove(os.path.join(st_dir, "docker.daemondown"))
        except Exception:
            pass
        marker = os.path.join(st_dir, "docker.degraded")
        if os.path.exists(marker):
            for f in (marker, os.path.join(st_dir, "docker.restart.ev")):
                try:
                    os.remove(f)
                except Exception:
                    pass
            _sw_log(st_dir, "docker daemon recovered from DEGRADED (probe healthy) - auto-recovery resumed")
        return True
    sf = os.path.join(st_dir, "docker.daemondown")
    n = 0
    try:
        n = int((open(sf).read().strip() or "0"))
    except Exception:
        n = 0
    n += 1
    try:
        with open(sf, "w") as fh:
            fh.write(str(n))
    except Exception:
        pass
    _sw_log(st_dir, "docker daemon unreachable (strike %d/%d) - docker-dependent recovery skipped this pass"
            % (n, SW_DOCKER_DAEMON_STRIKES))
    if n >= SW_DOCKER_DAEMON_STRIKES:
        ev = _sw_cb_guard(st_dir, "docker", "docker.restart", 3600,
                          SW_DOCKER_RESTART_BUDGET, "docker daemon")
        if ev is not None:
            _sw_log(st_dir, "docker daemon unreachable twice -> systemctl restart docker (%d/%d this hour)"
                    % (ev, SW_DOCKER_RESTART_BUDGET))
            try:
                rrc, _ = run(["systemctl", "restart", "docker"], timeout=60)
            except Exception:
                rrc = 1
            if rrc == 0:
                _sw_log(st_dir, "docker restarted ok")
            else:
                _sw_log(st_dir, "docker restart FAILED")
            try:
                os.remove(sf)
            except Exception:
                pass
    return False


def selfwatch_pass_once(lock_fn=None):
    """One selfwatch iteration (test seam for selfwatch_loop). The recovery
    actions (per-instance checks + host-level guards) run only when this
    process holds the single-instance lock; the per-process agent HTTP
    self-check always runs. Returns the lock state. Never raises."""
    if lock_fn is None:
        lock_fn = sw_selfwatch_lock
    try:
        state, detail = lock_fn()
    except Exception:
        state, detail = "broken", {"st_dir": SELF_WATCH_DIR,
                                  "reason": "lock_fn raised"}
    if not isinstance(detail, dict):
        detail = {"st_dir": SELF_WATCH_DIR, "reason": "bad lock detail"}
    try:
        st_dir = detail.get("st_dir") or SELF_WATCH_DIR
        if state == "held":
            if detail.get("takeover"):
                _sw_lock_note_takeover(st_dir, detail["takeover"])
            # (agent 1.0.39) docker-daemon guard runs FIRST: while dockerd
            # is down, docker-dependent recovery (boot recovery, the
            # per-instance loop, disk auto-prune) is skipped — a dead daemon
            # must not be mistaken for dead containers. Docker-independent
            # guards (disk alert, log trimmer, loop pool, memory watch)
            # keep running during the outage.
            try:
                daemon_ok = sw_check_docker_daemon(st_dir=st_dir)
            except Exception:
                daemon_ok = True
            try:
                if daemon_ok:
                    # (agent 1.0.32) section 0 — host-boot fleet recovery
                    # runs FIRST: binder pool restore + staggered starts,
                    # so the per-instance container-down check below
                    # doesn't start a boot storm.
                    try:
                        boot_status = sw_boot_recovery(st_dir=st_dir)
                    except Exception:
                        boot_status = "error"
                    try:
                        for name in list(load_instances().keys()):
                            try:
                                selfwatch_check_instance(name, st_dir=st_dir,
                                                         boot_recovery=boot_status)
                            except Exception:
                                pass
                    except Exception:
                        pass
                # host-level guards (agent 1.0.7): disk auto-recovery +
                # json-log trimmer, once per pass — not per instance
                # host-level guards (agent 1.0.12): loop device pool
                # self-heal, once per pass — not per instance
                # host-level guard (agent 1.0.13): memory-pressure watch +
                # OOM surfacing (alert-only), once per pass — not per
                # instance
                for guard in (sw_trim_container_logs,
                              sw_ensure_loop_devices, sw_check_memory):
                    try:
                        guard()
                    except Exception:
                        pass
                try:
                    sw_check_disk(docker_down=not daemon_ok)
                except Exception:
                    pass
            except Exception:
                pass
        elif state == "foreign":
            _sw_lock_note_foreign(st_dir, detail.get("pid") or "?")
        else:
            _sw_lock_note_broken(st_dir, detail.get("reason") or "?")
        # host-level guard (agent 1.0.17): agent HTTP self-check
        # (main-node df-watchdog v5/v12 parity) — a wedged listener is
        # invisible to systemd; 2 consecutive failed probes exit the
        # process so systemd revives it. Per-process: runs regardless of
        # the lock.
        try:
            sw_check_agent_http()
        except Exception:
            pass
    except Exception:
        pass
    return state


def selfwatch_loop():
    if IS_WINDOWS:
        return
    while True:
        try:
            selfwatch_pass_once()
        except Exception:
            pass
        time.sleep(SELF_WATCH_INTERVAL)

def build_heartbeat_payload():
    stats = get_host_stats()
    payload = {"provider_id": PROVIDER_ID, "agent_key": AGENT_KEY,
               "stats": stats, "version": VERSION,
               "port": BOUND_PORT,
               "instances": list(load_instances().keys())}
    # 1.0.27: host capability facts for the Nodes modal (image built?,
    # loop pool size). Computed inline: loop count is a /dev listing and
    # the image check is one 15s-timeout docker call per 30s heartbeat —
    # never blocks the heartbeat (helpers never raise, timeouts bounded).
    try:
        payload["host_caps"] = sw_host_caps()
    except Exception:
        pass
    if not IS_WINDOWS:
        payload["selfwatch"] = selfwatch_state()
    return payload


def heartbeat_loop():
    if not MAIN_URL or not PROVIDER_ID or not AGENT_KEY:
        return
    url = f"{MAIN_URL}/api/providers/heartbeat"
    while True:
        try:
            payload = build_heartbeat_payload()
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                # 1.0.24: read the response — the manager advertises a newer
                # agent.py here when one is deployed beside it. The response
                # was discarded before; a read failure must never break the
                # heartbeat itself.
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read(65536)
                try:
                    hb = json.loads(body.decode("utf-8", errors="replace"))
                except Exception:
                    hb = None
                if isinstance(hb, dict):
                    maybe_agent_self_update(hb.get("agent_update"))
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(30)


# ---- agent self-update (1.0.24) ------------------------------------------------
# The dashboard's Nodes modal badges a node whose agent is older than the one
# deployed beside the manager (wing-agent-update), but every bump still needed
# Penta to re-run the node installer by hand — twenty versions were pending at
# once. The agent now updates itself: the heartbeat response carries
# {"agent_update": {"version", "sha256"}} when a newer agent.py sits beside
# the manager; the agent pulls the file from its OWN manager (the MAIN_URL it
# already trusts and heartbeats to), verifies the sha256, byte-compiles it,
# swaps it in atomically (one .bak kept), and exits so the supervisor
# (systemd) restarts it on the new code.
# Gates (all fail-closed; every helper best-effort, never raises):
#   * Linux only — Windows nodes run as a scheduled task with no supervisor.
#   * Only under systemd (sw_under_systemd): without a supervisor, exiting
#     would orphan the node. One WARN per day, never silent.
#   * Strict version parse on both sides; the directive version must be
#     strictly NEWER (downgrades and re-installs of the same version never
#     apply).
#   * sha256 of the download must match the directive; py_compile must pass.
#   * At most one update attempt per process (no update loops).
#   * Download bounded at AGENT_UPDATE_MAX_BYTES; temp file removed unless
#     the swap succeeded.
AGENT_UPDATE_MAX_BYTES = 2 * 1024 * 1024
AGENT_UPDATE_TIMEOUT = 30
_UPDATE_ATTEMPTED = False
_UPDATE_RETRY_AFTER = 0.0
_UPDATE_RETRY_COOLDOWN = 600  # transient fetch/apply failures retry at most
# every 10 min (no tight failure loop, no silent permanent skip)


def parse_agent_version(s):
    """Strict dotted-version parse -> tuple of ints, or None when
    unparseable. Mirrors the manager's _parse_agent_version so both sides
    agree on ordering (1.0.9 < 1.0.10, not string order)."""
    try:
        parts = [int(x) for x in re.split(r"\D+", str(s).strip()) if x != ""]
        return tuple(parts) if parts else None
    except Exception:
        return None


def fetch_agent_update(directive, main_url=None, provider_id=None,
                       agent_key=None, dest_dir=None):
    """Download + verify a newer agent.py from the manager.

    Returns (True, tmp_path) with a byte-compiled temp file, or
    (False, reason). The caller swaps it in via apply_agent_update().
    Never raises.
    """
    try:
        if not isinstance(directive, dict):
            return (False, "bad directive")
        if not parse_agent_version(directive.get("version")):
            return (False, "bad version")
        sha = directive.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            return (False, "bad sha256")
        base = (main_url or MAIN_URL or "").rstrip("/")
        if not base:
            return (False, "no manager url")
        url = base + "/api/agent/update/download"
        req = urllib.request.Request(
            url,
            headers={"X-Provider-Id": provider_id or PROVIDER_ID,
                     "X-Agent-Key": agent_key or AGENT_KEY,
                     "Connection": "close"})
        with urllib.request.urlopen(req,
                                    timeout=AGENT_UPDATE_TIMEOUT) as resp:
            if resp.getcode() != 200:
                return (False, "http %s" % (resp.getcode(),))
            data = resp.read(AGENT_UPDATE_MAX_BYTES + 1)
        if len(data) > AGENT_UPDATE_MAX_BYTES:
            return (False, "too large")
        if hashlib.sha256(data).hexdigest() != sha:
            return (False, "sha256 mismatch")
        if dest_dir is None:
            dest_dir = os.path.dirname(os.path.abspath(__file__))
        fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix="agent-update-",
                                   suffix=".py")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            import py_compile
            py_compile.compile(tmp, doraise=True)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return (False, "compile failed")
        return (True, tmp)
    except Exception as e:
        return (False, "error: %s" % (e,))


def apply_agent_update(tmp_path, agent_path=None):
    """Back up the current agent.py (one .bak) and atomically swap in the
    verified download. Returns True/False. Never raises."""
    try:
        if agent_path is None:
            agent_path = os.path.abspath(__file__)
        if os.path.exists(agent_path):
            try:
                shutil.copy2(agent_path, agent_path + ".bak")
            except Exception:
                return False
        os.replace(tmp_path, agent_path)
        return True
    except Exception:
        return False


def _update_nosystemd_note(st_dir):
    """One WARN per day when self-update is skipped for lack of a
    supervisor. Returns True when the note was written (never raises)."""
    try:
        dayf = os.path.join(st_dir,
                            "agent.update.nosystemd." + time.strftime("%F"))
        if os.path.exists(dayf):
            return False
        for fn in os.listdir(st_dir):
            if fn.startswith("agent.update.nosystemd."):
                try:
                    os.remove(os.path.join(st_dir, fn))
                except Exception:
                    pass
        open(dayf, "w").close()
        _sw_log(st_dir, "WARN agent self-update skipped: not running under "
                "systemd (no supervisor would restart the process)")
        return True
    except Exception:
        return False


def maybe_agent_self_update(directive, _env=None, _exit_fn=None, _fetch=None,
                            _apply=None, _st_dir=None):
    """One self-update pass. Returns a status string; the process exits
    (via _exit_fn, default os._exit) only AFTER a verified swap. The
    underscore overrides exist for unit tests. Never raises."""
    global _UPDATE_ATTEMPTED, _UPDATE_RETRY_AFTER
    try:
        st_dir = _st_dir or SELF_WATCH_DIR
        if IS_WINDOWS:
            return "skipped-windows"
        if _UPDATE_ATTEMPTED:
            # A failed attempt retries after a cooldown (transient network
            # blips self-heal); a success exits the process, so the flag is
            # never cleared on this path.
            if time.time() < _UPDATE_RETRY_AFTER:
                return "skipped-cooldown"
        if not isinstance(directive, dict):
            return "no-directive"
        want = parse_agent_version(directive.get("version"))
        have = parse_agent_version(VERSION)
        if not want or not have:
            return "skipped-version"
        if want <= have:
            return "current"
        if not sw_under_systemd(_env):
            _update_nosystemd_note(st_dir)
            return "skipped-nosystemd"
        _UPDATE_ATTEMPTED = True
        fetch = _fetch or fetch_agent_update
        ok, info = fetch(directive)
        if not ok:
            _UPDATE_RETRY_AFTER = time.time() + _UPDATE_RETRY_COOLDOWN
            _sw_log(st_dir, "WARN agent self-update fetch failed: %s"
                    % (info,))
            return "fetch-failed"
        apply = _apply or apply_agent_update
        if not apply(info):
            _UPDATE_RETRY_AFTER = time.time() + _UPDATE_RETRY_COOLDOWN
            _sw_log(st_dir, "WARN agent self-update apply failed")
            try:
                os.remove(info)
            except Exception:
                pass
            return "apply-failed"
        _sw_log(st_dir, "ALERT agent self-updated %s -> %s, restarting "
                "for supervisor" % (VERSION, directive.get("version")))
        note_agent_update(st_dir, directive.get("version"), VERSION)
        if _exit_fn is None:
            os._exit(1)
        else:
            _exit_fn(1)
        return "updated"
    except Exception:
        return "error"


# ---- self-update rollback (1.0.30) ------------------------------------------
# maybe_agent_self_update() swaps in a sha256-verified, py_compile-clean
# agent.py and exits. py_compile catches syntax errors, but a runtime bug
# (a module-level NameError, a bad import, a crash inside main()) slips
# through and crash-loops — under systemd's Restart=always AND the
# __main__ while-True loop — with no recovery. The agent IS the node's
# recovery mechanism, so a crash-looping agent is a dead node until a
# human intervenes: exactly what self-update was built to avoid.
#
# The fix: a successful update leaves a dated marker; every startup
# records a boot timestamp; a boot storm (>= ROLLBACK_BOOTS startups
# inside ROLLBACK_WINDOW) while the marker is fresh (<=
# ROLLBACK_MARKER_TTL after the update) restores agent.py.bak — the code
# that was running minutes ago — and re-execs it. The marker and boot
# log are cleared on rollback, so a broken ENVIRONMENT (not a broken
# update) can never rollback-loop: worst case the node crash-loops on
# the old code, exactly as it would today with no rollback. The marker
# is only ever written by the update path, so nodes that never
# self-updated (Windows included) are unaffected. All helpers
# best-effort, never raise.
UPDATE_MARKER = "agent.update.marker"   # JSON {version, from, ts}
UPDATE_BOOTLOG = "agent.boot.log"       # one epoch per line, pruned
ROLLBACK_BOOTS = 4          # >= this many startups ...
ROLLBACK_WINDOW = 300       # ... inside this many seconds ...
ROLLBACK_MARKER_TTL = 1800  # ... and only within 30 min of an update


def _update_marker_path(st_dir):
    return os.path.join(st_dir, UPDATE_MARKER)


def _update_bootlog_path(st_dir):
    return os.path.join(st_dir, UPDATE_BOOTLOG)


def note_agent_update(st_dir, version, from_version):
    """Record a successful self-update (called before the restart exit).
    Atomic tmp+rename; resets the boot log so the new code's first boots
    are counted fresh. Returns True/False. Never raises."""
    try:
        os.makedirs(st_dir, exist_ok=True)
        marker = _update_marker_path(st_dir)
        tmp = marker + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": str(version), "from": str(from_version),
                       "ts": time.time()}, f)
        os.replace(tmp, marker)
        try:
            os.remove(_update_bootlog_path(st_dir))
        except OSError:
            pass
        return True
    except Exception:
        return False


def record_agent_boot(st_dir, now=None):
    """Append one boot timestamp, pruning entries older than
    ROLLBACK_WINDOW. Returns the pruned list of epochs. Never raises."""
    try:
        now = time.time() if now is None else float(now)
    except Exception:
        now = time.time()
    path = _update_bootlog_path(st_dir)
    boots = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    b = float(line.strip())
                except ValueError:
                    continue
                if 0 < b <= now + 1.0 and now - b <= ROLLBACK_WINDOW:
                    # +1.0s: the boot log is written at %.3f precision, so a
                    # timestamp stored a fraction of a ms "in the future"
                    # by rounding must not be dropped as clock skew.
                    boots.append(b)
    except (OSError, ValueError):
        # ValueError: hostile path (embedded null byte) — treated as
        # unreadable, same as a missing file.
        pass
    boots.append(now)
    try:
        os.makedirs(st_dir, exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join("%.3f" % b for b in boots) + "\n")
    except Exception:
        pass
    return boots


def _read_update_marker(st_dir):
    """The update marker dict, or None when missing/corrupt/expired."""
    try:
        with open(_update_marker_path(st_dir)) as f:
            m = json.load(f)
        if not isinstance(m, dict):
            return None
        ts = m.get("ts")
        if not isinstance(ts, (int, float)) or ts <= 0:
            return None
        if time.time() - ts > ROLLBACK_MARKER_TTL:
            return None
        return m
    except Exception:
        return None


def maybe_rollback_agent_update(_st_dir=None, _agent_path=None, _execv=None):
    """Startup crash-loop guard. Call at the top of main().

    Returns a status string. On a confirmed boot storm after a fresh
    update it restores agent.py.bak (compile-checked first, fail-closed),
    clears the marker + boot log, logs one ALERT, and re-execs the
    restored file (falls back to os._exit(1) so the supervisor restarts
    into the restored file). Never raises."""
    st_dir = _st_dir or SELF_WATCH_DIR
    try:
        boots = record_agent_boot(st_dir)
        marker = _read_update_marker(st_dir)
        if marker is None:
            return "no-marker"
        if len(boots) < ROLLBACK_BOOTS:
            return "ok"
        agent_path = _agent_path or os.path.abspath(__file__)
        bak = agent_path + ".bak"
        reason = None
        if not os.path.isfile(bak):
            reason = "no .bak to roll back to"
        else:
            try:
                import py_compile
                py_compile.compile(bak, doraise=True)
            except Exception:
                reason = ".bak failed compile check"
        if reason is None:
            try:
                shutil.copy2(bak, agent_path)
            except Exception:
                reason = "could not restore .bak"
        # The marker is single-use: clear it whatever happened, so a
        # broken environment can never rollback-loop.
        try:
            os.remove(_update_marker_path(st_dir))
        except OSError:
            pass
        try:
            os.remove(_update_bootlog_path(st_dir))
        except OSError:
            pass
        if reason is not None:
            _sw_log(st_dir, "ALERT agent update rollback skipped: %s "
                    "(update was %s, node keeps crash-looping)"
                    % (reason, marker.get("version")))
            return "no-backup"
        _sw_log(st_dir, "ALERT agent rolled back %s -> %s after %d boots "
                "in %ds; restarting on restored code"
                % (marker.get("version"), marker.get("from"),
                   len(boots), ROLLBACK_WINDOW))
        argv = [sys.executable, agent_path] + sys.argv[1:]
        if _execv is not None:
            _execv(sys.executable, argv)
            return "rolled-back"
        try:
            os.execv(sys.executable, argv)
        except Exception:
            pass
        try:
            os._exit(1)
        except Exception:
            pass
        return "rolled-back"
    except Exception:
        return "error"


# ---- agent background-thread supervisor --------------------------------------
# The manager supervises its own background threads (auto-login watcher,
# resource poller, webrtc-loop) since 2026-10-11, but the agent's two
# background threads ran unsupervised: a dead selfwatch_loop silently
# disabled ALL node-side auto-recovery (helper restarts, container starts,
# stale-stream fixes, disk prune, log trim, drift checks) while heartbeats
# kept flowing, so the dashboard showed a healthy node with silently dying
# instances; a dead heartbeat_loop went the other way (the manager marks the
# node offline and every card flickers to "stopped") while the agent's HTTP
# server kept answering. This mirrors the manager's register_supervised
# contract: reaps + restarts dead registered threads every
# ASUPERVISOR_INTERVAL seconds, with a circuit breaker (>3 restarts/hour ->
# give up + one ALERT, needs a human). Restarts and give-ups are appended to
# selfwatch.log so the dashboard "Eventos" modal surfaces them (the manager
# applies its canonical severity tagging to node events, so no manager
# changes are needed). Everything here is best-effort and never raises.
_ASUPERVISED = {}
_asupervised_lock = threading.Lock()
ASUPERVISOR_INTERVAL = 30
ASUPERVISOR_MAX_RESTARTS = 3
ASUPERVISOR_WINDOW = 3600


def _asup_log(msg):
    """Append one line to the agent selfwatch log (Eventos modal reads it
    via /api/agent/selfwatch-events). Best-effort, never raises."""
    try:
        _sw_log(SELF_WATCH_DIR, msg)
    except Exception:
        pass


def register_supervised(name, factory):
    """Register a background thread for supervision. factory() must return an
    unstarted threading.Thread. Best-effort, never raises."""
    try:
        with _asupervised_lock:
            _ASUPERVISED[name] = {"factory": factory, "thread": None,
                                  "restarts": [], "given_up": False}
    except Exception:
        pass


def _asupervise_entry(name, entry):
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
    recent = [ts for ts in entry.get("restarts", []) if now - ts < ASUPERVISOR_WINDOW]
    entry["restarts"] = recent
    if len(recent) >= ASUPERVISOR_MAX_RESTARTS:
        entry["given_up"] = True
        _asup_log("ALERT supervisor gave up restarting '%s' "
                  "(%d restarts in the last hour) - needs human attention"
                  % (name, ASUPERVISOR_MAX_RESTARTS))
        return
    try:
        nt = entry["factory"]()
        nt.start()
    except Exception as ex:
        _asup_log("WARN supervisor failed to (re)start '%s': %s" % (name, ex))
        return
    entry["thread"] = nt
    recent.append(now)
    entry["restarts"] = recent
    _asup_log("WARN supervisor started background thread '%s'" % name)


def _asupervise_once():
    """One supervisor pass over all registered threads. Never raises."""
    try:
        with _asupervised_lock:
            items = list(_ASUPERVISED.items())
    except Exception:
        return
    for name, entry in items:
        try:
            _asupervise_entry(name, entry)
        except Exception:
            pass


def agent_thread_supervisor():
    """Daemon loop: keep registered background threads alive. Never raises."""
    while True:
        try:
            _asupervise_once()
        except Exception:
            pass
        time.sleep(ASUPERVISOR_INTERVAL)


def _heartbeat_thread_factory():
    return threading.Thread(target=heartbeat_loop, daemon=True,
                            name="agent-heartbeat")


def _selfwatch_thread_factory():
    return threading.Thread(target=selfwatch_loop, daemon=True,
                            name="agent-selfwatch")


def _supervisor_registrations():
    """(name, factory) pairs main() registers. heartbeat_loop returns
    immediately when the agent is unconfigured, and selfwatch_loop returns
    immediately on Windows (selfwatch is Linux-only) - registering either
    would burn the restart budget and ALERT on a legitimately idle thread,
    so both are skipped."""
    regs = []
    if MAIN_URL and PROVIDER_ID and AGENT_KEY:
        regs.append(("heartbeat", _heartbeat_thread_factory))
    if not IS_WINDOWS:
        regs.append(("selfwatch", _selfwatch_thread_factory))
    return regs


BOUND_PORT = PORT


def main():
    global BOUND_PORT, _AGENT_HTTP_BOUND
    # Self-update rollback guard (1.0.30): a boot storm after a fresh
    # update restores agent.py.bak and re-execs it. Runs first so even a
    # crash inside main()'s body counts toward the storm.
    maybe_rollback_agent_update()
    # Stale b64 input staging sweep (remote-b64-sweep): a previous agent
    # process may have died inside the 60s cleanup window (crash,
    # self-update, systemd restart), leaving a staged secret — often a
    # Roblox login password — in an instance volume. No live stage can
    # exist yet (the server is not bound), so sweep unconditionally,
    # before any thread starts. Best-effort, never raises.
    sweep_stale_b64_input_text()
    # The supervisor's first pass starts the registered threads immediately,
    # so startup behavior is unchanged vs the old direct .start() calls;
    # death -> restart within 30s, >3 restarts/hour -> give up + one ALERT
    # in selfwatch.log (surfaced by the dashboard Eventos modal).
    for name, factory in _supervisor_registrations():
        register_supervised(name, factory)
    sup = threading.Thread(target=agent_thread_supervisor, daemon=True,
                           name="agent-supervisor")
    sup.start()
    port = PORT
    srv = None
    for _ in range(10):
        try:
            srv = BoundedHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError:
            port += 1
    if srv is None:
        time.sleep(30)
        return
    BOUND_PORT = port
    _AGENT_HTTP_BOUND = True  # the selfwatch HTTP self-check may now probe
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        time.sleep(5)


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception:
            time.sleep(10)
