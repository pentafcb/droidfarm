#!/usr/bin/env python3
"""
DroidFarm instance manager + dashboard backend.
Stdlib-only (http.server + manual WebSocket). Manages Waydroid-in-Docker
instances on the host via the docker CLI.

Env:
  DROIDFARM_PASSWORD  dashboard password (default: changeme - change it!)
  DROIDFARM_PORT      listen port (default 8080)
  DROIDFARM_DATA      data dir (default /root/droidfarm)
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
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

DATA_DIR = os.environ.get("DROIDFARM_DATA", "/root/droidfarm")
PASSWORD = os.environ.get("DROIDFARM_PASSWORD", "changeme")

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
PORT = int(os.environ.get("DROIDFARM_PORT", "8080"))
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
DROID_SCRATCH = "/data/media/0/droidfarm"
HOST_SCRATCH = "/root/.local/share/waydroid/data/media/0/droidfarm"

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
    except FileNotFoundError:
        pass
    t = secrets.token_hex(32)
    with open(STATIC_TOKEN_FILE, "w") as f:
        f.write(t)
    try:
        os.chmod(STATIC_TOKEN_FILE, 0o600)
    except OSError:
        pass
    return t


def load_providers():
    try:
        with open(PROVIDERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_providers(data):
    tmp = PROVIDERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, PROVIDERS_FILE)


def gen_secret(n=32):
    return secrets.token_urlsafe(n)


def wing_request(provider, method, path, data=None, timeout=8):
    """HTTP request to a wing agent. Returns (status_code, parsed_json_or_bytes)."""
    import urllib.request as _ureq
    import urllib.error as _uerr
    import http.client as _httpc
    url = provider["url"].rstrip("/") + path
    body = json.dumps(data).encode() if data is not None else None
    last_err = None
    for attempt in range(3):
        req = _ureq.Request(url, data=body, method=method,
                            headers={"X-Agent-Key": provider["agent_key"],
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
            if attempt < 2:
                time.sleep(1)
                continue
            return 502, {"error": f"node unreachable: {e}"}
        except Exception as e:
            return 502, {"error": f"node unreachable: {e}"}
    return 502, {"error": f"node unreachable: {last_err}"}


def pick_provider(providers, need_ram_mb=4096, need_cpus=2):
    """Auto-pick the least-loaded provider with enough free capacity.
    'local' is always a candidate."""
    cands = []
    # local capacity
    ls = get_host_stats()
    local_used_ram = sum(v.get("memory_mb", 0) for v in load_instances().values()
                         if v.get("provider", "local") == "local")
    if ls["ram_total_mb"] - local_used_ram >= need_ram_mb:
        cands.append(("local", ls["ram_percent"]))
    for pid, p in providers.items():
        st = p.get("stats") or {}
        total = st.get("ram_total_mb", 0)
        if not total:
            continue
        # rough used = reported percent; instances on that provider tracked by main
        used_by_us = sum(v.get("memory_mb", 0) for v in load_instances().values()
                         if v.get("provider") == pid)
        free_mb = total * (1 - st.get("ram_percent", 0) / 100) - used_by_us
        # provider must have heartbeated recently
        if time.time() - p.get("last_seen", 0) > 120:
            continue
        if free_mb >= need_ram_mb:
            cands.append((pid, st.get("ram_percent", 0)))
    if not cands:
        return None
    cands.sort(key=lambda x: x[1])
    return cands[0][0]


def load_instances():
    try:
        with open(INSTANCES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_instances(data):
    tmp = INSTANCES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, INSTANCES_FILE)


def load_accounts():
    """Load Roblox account credentials per instance. Format: {name: {username, password, auto_login}}"""
    try:
        with open(ACCOUNTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_accounts(data):
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, ACCOUNTS_FILE)
    try:
        os.chmod(ACCOUNTS_FILE, 0o600)
    except OSError:
        pass


def load_login_results():
    """Last recorded Roblox login attempt per instance.

    Format: {name: {ok, ts, reason, by, attempts}}. Outcome only —
    never stores credentials.
    """
    try:
        with open(LOGIN_RESULTS_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_login_results(data):
    tmp = LOGIN_RESULTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, LOGIN_RESULTS_FILE)
    try:
        os.chmod(LOGIN_RESULTS_FILE, 0o600)
    except OSError:
        pass


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


# ---- bulk Roblox account import -------------------------------------------
# Text format, one instance per line:
#   name,username,password[,auto]
# Lines starting with # and blank lines are skipped. The optional 4th field
# enables auto_login (accepts 1/true/yes/auto/y, case-insensitive).

_AUTO_TRUE = {"1", "true", "yes", "auto", "y"}


def parse_bulk_accounts(text):
    """Parse bulk import text. Returns (entries, line_errors).

    entries: [{name, username, password, auto_login}]
    line_errors: [{line, error}] for lines that could not be parsed."""
    entries, line_errors = [], []
    for lineno, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            line_errors.append({"line": lineno,
                                "error": "need name,username,password"})
            continue
        name, username, password = parts[0], parts[1], parts[2]
        auto = len(parts) > 3 and parts[3].lower() in _AUTO_TRUE
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
        acct = accounts.get(e["name"], {})
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
        acct = accounts.get(name, {})
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
            _tokens = {t: e for t, e in json.load(f).items() if e > time.time()}
    except (FileNotFoundError, json.JSONDecodeError):
        _tokens = {}


def save_tokens():
    try:
        with open(TOKENS_FILE, "w") as f:
            json.dump(_tokens, f)
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


def docker_inspect(name):
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
    """Watchdog circuit-breaker state for a local instance.

    df-watchdog.sh (minute cron) writes <name>.degraded into DF_ST when an
    instance keeps failing and auto-recovery pauses. The marker's first line
    is the human-readable reason (v4.1+); older/empty markers get a generic
    message. Surfacing this lets the dashboard show "needs attention" instead
    of a misleading green "ready" card. Best-effort: never raises.
    """
    try:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(name))
        st = os.environ.get("DF_ST", "/tmp/df-watchdog")
        p = os.path.join(st, safe + ".degraded")
        if not os.path.isfile(p):
            return {"degraded": False, "reason": None}
        reason = None
        try:
            with open(p, "r") as f:
                line = f.readline().strip()
                if line:
                    reason = line[:200]
        except OSError:
            pass
        return {"degraded": True,
                "reason": reason or "auto-recovery paused by watchdog"}
    except Exception:
        return {"degraded": False, "reason": None}


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


def resource_poller():
    """Background loop: never dies, never raises."""
    while True:
        try:
            _resource_poll_once()
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
    except (TypeError, ValueError):
        return None
    if not 0 <= f <= 1e6:
        return None
    return round(f, 1)


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


def fleet_resource_summary():
    """Actual fleet-wide usage from the cache (not limits): for the dashboard."""
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
    host_mb = None
    try:
        host_mb = host_mem_total_mb()
    except Exception:
        pass  # this feeds every /api/instances response; it must never raise
    return {"mem_used_mb": round(used, 1), "cpu_pct": round(cpu, 1),
            "instances_seen": n, "host_mem_mb": host_mb}


def next_instance_id(instances):
    """Allocate the next free INSTANCE_ID (unique int per instance)."""
    used = {inst.get("instance_id") for inst in instances.values()
            if isinstance(inst.get("instance_id"), int)}
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
    Returns True if at least one device was created.
    """
    # Make sure binderfs is mounted.
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
        for b in inst.get("binders", []):
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
    used = {inst.get("ip") for inst in instances.values() if inst.get("ip")}
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
    cname = f"wd-{name}"
    script = f"input tap 0 0"  # placeholder, replaced below
    # Write script via a python one-liner to avoid quoting hell
    import shlex
    payload = base64.b64encode(android_cmd.encode()).decode()
    setup = (
        f"docker exec {cname} mkdir -p {HOST_SCRATCH} && "
        f"docker exec {cname} python3 -c "
        f"\"import base64; open('{HOST_SCRATCH}/cmd.sh','w').write(base64.b64decode('{payload}').decode())\""
    )
    rc, _, _ = run(setup, timeout=15)
    if rc != 0:
        return rc, ""
    rc, out = run_bytes(
        f"docker exec {cname} waydroid shell sh {DROID_SCRATCH}/cmd.sh",
        timeout=timeout)
    return rc, out.decode(errors="replace")


def check_roblox(name):
    rc, out = waydroid_exec(name, "pm list packages | grep com.roblox.client", timeout=25)
    return "com.roblox.client" in out


def _send_input_cmds(name, cmds):
    """Send input commands via FIFO (fast) or waydroid_exec (slow fallback).
    Commands are joined into ONE line per FIFO open: the input daemon does
    `read cmd < $FIFO` (reopening after every line), so separate writes can
    be lost or split. One compound write is atomic for the reader."""
    cname = f"wd-{name}"
    fifo_path = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/droidfarm/input.fifo"
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


def launch_roblox(name):
    """Launch Roblox by tapping its icon in the app drawer.
    Returns True if the tap sequence was sent."""
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


def _wing_input(provider_meta, name, payload, timeout=15):
    """Send one input payload to a remote node's wing agent."""
    code, resp = wing_request(provider_meta, "POST",
                             f"/api/agent/instances/{name}/input",
                             payload, timeout=timeout)
    ok = 200 <= code < 300
    return ok, resp if isinstance(resp, dict) else {"ok": ok}


def _remote_started_at(provider_meta, name):
    """Container StartedAt epoch for a remote instance (0 if unknown)."""
    code, st = wing_request(provider_meta, "GET",
                            f"/api/agent/instances/{name}/status", timeout=10)
    if 200 <= code < 300 and isinstance(st, dict):
        try:
            return int(st.get("started_at") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _roblox_login_remote(provider, name, username, password):
    """Roblox login sequence on a remote node via the wing agent.
    Needs agent >= 1.0.2 (text input endpoint). Sleeps run manager-side
    between stateless agent calls. Returns (ok, message)."""
    p = load_providers().get(provider)
    if not p:
        return False, "node not found"
    # Agent must be new enough to type text
    code, health = wing_request(p, "GET", "/api/agent/health", timeout=10)
    ver = health.get("version", "") if isinstance(health, dict) else ""
    if not (200 <= code < 300) or not _version_at_least(ver, "1.0.2"):
        return False, ("node agent too old for auto-login (needs 1.0.2+) - "
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


def _roblox_login_inner(name):
    """The login sequence itself. Returns (ok, message). Use roblox_login()
    instead — it records the outcome for dashboard visibility."""
    accounts = load_accounts()
    acct = accounts.get(name)
    if not acct or not acct.get("username") or not acct.get("password"):
        return False, "no credentials saved for this instance"

    username = acct["username"]
    password = acct["password"]
    provider = load_instances().get(name, {}).get("provider", "local")
    if provider != "local":
        return _roblox_login_remote(provider, name, username, password)

    cname = f"wd-{name}"
    
    # Ensure Roblox is in foreground
    _send_input_cmds(name, ["input keyevent 3", "sleep 1"])
    launch_roblox(name)
    time.sleep(10)  # Wait for Roblox to load to login screen
    
    # Write password to file via base64 to avoid shell special char issues
    pwd_b64 = base64.b64encode(password.encode()).decode()
    vol_path = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/droidfarm"
    try:
        with open(f"{vol_path}/pwd.txt", "wb") as f:
            f.write(base64.b64decode(pwd_b64))
    except OSError:
        return False, "could not write password file"
    
    # Login sequence using verified coordinates (1024x608)
    # Username field: (512, 228), Password: (512, 296), Sign In: (512, 367)
    # Escape username for shell (password is via file)
    safe_user = username.replace(" ", "%s").replace("'", "").replace('"', "")
    
    cmds = [
        "input tap 512 228",  # username field
        "sleep 2",
        f"input text {safe_user}",
        "sleep 1",
        "input tap 512 296",  # password field
        "sleep 2",
        'P=$(cat /data/media/0/droidfarm/pwd.txt); input text "$P"',
        "sleep 2",
        "input tap 512 367",  # Sign In button
    ]
    
    # Send via waydroid shell script (more reliable than FIFO for multi-command)
    script_content = "\n".join(cmds)
    script_path = f"{vol_path}/auto_login.sh"
    try:
        with open(script_path, "w") as f:
            f.write(script_content)
    except OSError:
        return False, "could not write login script"
    
    rc, out = waydroid_exec(name, "sh /data/media/0/droidfarm/auto_login.sh 2>&1", timeout=60)
    # Clean up password file
    try:
        os.remove(f"{vol_path}/pwd.txt")
    except OSError:
        pass
    
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


def _auto_login_run(name):
    try:
        ok, msg = roblox_login(name, by="auto")
        print(f"[auto-login] {name}: ok={ok} {msg}", flush=True)
    except Exception as e:
        print(f"[auto-login] {name} exception: {e}", flush=True)


def auto_login_watcher():
    """Background thread: auto-login to Roblox on first boot for instances
    with auto_login enabled. Bounded: first-boot window (30 min after creation)
    only, max 3 attempts. Restarts are skipped (Roblox session persists in the
    volume), the dashboard Login button covers edge cases. Works for local and
    remote-node instances."""
    time.sleep(90)  # let the server and any in-flight boots settle
    while True:
        try:
            accounts = load_accounts()
            instances = load_instances()
            providers = load_providers()
            for name, acct in list(accounts.items()):
                if not isinstance(acct, dict) or not acct.get("auto_login"):
                    continue
                if not acct.get("username") or not acct.get("password"):
                    continue
                inst = instances.get(name)
                if not inst:
                    continue
                provider = inst.get("provider", "local")
                if provider == "local":
                    cname = f"wd-{name}"
                    started = _container_started_at(cname)
                else:
                    p = providers.get(provider)
                    if not p:
                        continue
                    started = _remote_started_at(p, name)
                if not started or acct.get("login_boot") == started:
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
                acct["login_attempts"] = attempts + 1
                save_accounts(accounts)
                threading.Thread(target=_auto_login_run, args=(name,),
                                 daemon=True, name=f"auto-login-{name}").start()
        except Exception:
            pass
        time.sleep(60)


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


def ws_read_frame(sock):
    """Read one client frame; return (opcode, payload) or (None, None) on close/error."""
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
    except (OSError, struct.error):
        return None, None


_local_shot_cache = {}  # name -> (mtime, jpeg_bytes)

def capture_screenshot(name):
    """Return JPEG bytes of the instance screen, or None.
    Reads directly from the docker volume (zero exec overhead).
    The container's entrypoint runs a persistent screencap loop writing
    to /data/media/0/droidfarm/live.png inside the volume."""
    cname = f"wd-{name}"
    vol_path = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/droidfarm/live.png"
    try:
        # Wait briefly for a fresh frame (avoid reading a partially-written file)
        for _ in range(3):
            if os.path.exists(vol_path):
                break
            time.sleep(0.2)
        if not os.path.exists(vol_path):
            return None
        mtime = os.path.getmtime(vol_path)
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
_webrtc_pcs = {}  # pc_id -> RTCPeerConnection

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
            code, data = wing_request(wing, "GET",
                                     f"/api/agent/instances/{name}/screenshot",
                                     timeout=10)
            if code == 200 and isinstance(data, bytes) and len(data) > 1000:
                jpeg = data
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


def webrtc_init():
    """Start the asyncio event loop for WebRTC in a background thread."""
    global _webrtc_loop
    if not HAS_AIORTC:
        return
    _webrtc_loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(_webrtc_loop)
        _webrtc_loop.run_forever()

    threading.Thread(target=run, daemon=True, name="webrtc-loop").start()


def _get_webrtc_frame(name, provider):
    """Get raw RGB frame bytes (width, height, rgb_bytes) for WebRTC encoding."""
    jpeg = None
    if provider == "local":
        jpeg = capture_screenshot(name)
    else:
        wing = load_providers().get(provider)
        if wing:
            code, data = wing_request(wing, "GET",
                                     f"/api/agent/instances/{name}/screenshot",
                                     timeout=10)
            if code == 200 and isinstance(data, bytes) and len(data) > 1000:
                jpeg = data
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

        def __init__(self, instance_name, provider):
            super().__init__()
            self.instance_name = instance_name
            self.provider = provider
            self._last_rgb = None

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            # Run blocking screenshot fetch in executor
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, _get_webrtc_frame, self.instance_name, self.provider)
            if result:
                w, h, rgb = result
                self._last_rgb = (w, h, rgb)
            elif not self._last_rgb:
                await asyncio.sleep(0.1)
                return await self.recv()
            else:
                w, h, rgb = self._last_rgb
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
    _webrtc_pcs[pc_id] = pc

    @pc.on("connectionstatechange")
    async def on_state():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            _webrtc_pcs.pop(pc_id, None)
            try:
                await pc.close()
            except Exception:
                pass

    # Video track
    if HAS_AIORTC:
        pc.addTrack(ScreenVideoTrack(instance_name, provider))

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

    offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return pc.localDescription.sdp


def webrtc_offer_sync(instance_name, provider, offer_sdp, input_handler):
    """Sync wrapper: create WebRTC offer answer (called from HTTP handler)."""
    if not HAS_AIORTC or _webrtc_loop is None:
        return None
    fut = asyncio.run_coroutine_threadsafe(
        _webrtc_create_offer(instance_name, provider, offer_sdp, input_handler),
        _webrtc_loop)
    try:
        return fut.result(timeout=15)
    except Exception:
        return None


def do_instance_input(name, provider, data):
    """Execute an input command. Returns (ok_bool, result_dict).
    Usable from HTTP handler and WebRTC data channel."""
    itype = data.get("type")
    if provider != "local":
        p = load_providers().get(provider)
        if not p:
            return False, {"error": "node not found"}
        code, resp = wing_request(p, "POST", f"/api/agent/instances/{name}/input",
                                 data, timeout=15)
        return (200 <= code < 300), resp if isinstance(resp, dict) else {"error": "wing failed"}
    cname = f"wd-{name}"
    if container_status(cname) != "running":
        return False, {"error": "instance not running"}

    def scale(x, y):
        try:
            return int(float(x) / 1000 * DISP_W), int(float(y) / 1000 * DISP_H)
        except (TypeError, ValueError):
            return None

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
    fifo_path = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/droidfarm/input.fifo"
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


def instance_single_action(name, action):
    """Run one fleet action on one instance. Returns (http_code, dict).
    Shared by the per-instance endpoints and the batch worker, so behavior
    stays identical in both paths."""
    instances = load_instances()
    if name not in instances:
        return 404, {"error": "instance not found"}
    prov = instances[name].get("provider", "local")
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
        if not p:
            return 404, {"error": "node not found"}
        timeout = 30 if action == "launch-roblox" else 60
        if action == "restart":
            code, resp = wing_request(p, "POST",
                                     f"/api/agent/instances/{name}/stop", timeout=60)
            if code != 200:
                return code, resp if isinstance(resp, dict) else {"error": "wing failed"}
            code, resp = wing_request(p, "POST",
                                     f"/api/agent/instances/{name}/start", timeout=60)
            return code, resp if isinstance(resp, dict) else {"error": "wing failed"}
        code, resp = wing_request(p, "POST",
                                 f"/api/agent/instances/{name}/{action}", timeout=timeout)
        return code, resp if isinstance(resp, dict) else {"error": "wing failed"}
    cname = f"wd-{name}"
    if action in ("start", "stop"):
        rc, _, err = run(f"docker {'start' if action == 'start' else 'stop'} {cname}",
                         timeout=60)
        if rc != 0:
            return 500, {"error": err.strip() or "docker failed"}
        return 200, {"ok": True, "status": container_status(cname)}
    if action == "restart":
        rc, _, err = run(f"docker stop {cname}", timeout=60)
        if rc != 0:
            return 500, {"error": err.strip() or "docker failed"}
        rc, _, err = run(f"docker start {cname}", timeout=60)
        if rc != 0:
            return 500, {"error": err.strip() or "docker failed"}
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
        for jid in [jid for jid, job in _batch_jobs.items()
                    if now - job.get("created", now) > BATCH_TTL]:
            _batch_jobs.pop(jid, None)


def batch_create_job(action, names):
    """Validate and start a batch job. Returns (http_code, dict)."""
    if action not in BATCH_ACTIONS:
        return 400, {"error": "action must be one of: " + ", ".join(BATCH_ACTIONS)}
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
    threading.Thread(target=_run_batch_job, args=(jid,), daemon=True).start()
    return 202, {"job_id": jid, "action": action, "total": len(targets)}


def _run_batch_job(jid):
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
    with _lock:
        job = _batch_jobs.get(jid)
        if job:
            job["status"] = "done"


def batch_get_job(jid):
    with _lock:
        job = _batch_jobs.get(jid)
        if not job:
            return 404, {"error": "job not found"}
        view = {k: job[k] for k in ("job_id", "action", "status", "total", "done", "created")
                if k in job}
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


def create_local_instance(name, cpus, memory_mb, ip=None):
    """Create one instance on the main node. Returns (http_code, dict).
    Shared by the single-create endpoint and the fleet-create worker."""
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
    return 200, {"ok": True, "name": name, "ip": ip,
                 "binders": binders, "instance_id": instance_id}


def create_remote_instance(name, cpus, memory_mb, provider_id):
    """Create one instance on a wing node via the agent. Returns (http_code, dict).
    Shared by the single-create endpoint and the fleet-create worker."""
    p = load_providers().get(provider_id)
    if not p:
        return 404, {"error": "node not found"}
    if time.time() - p.get("last_seen", 0) > 120:
        return 409, {"error": "node offline"}
    with _lock:
        instances = load_instances()
        if name in instances:
            return 409, {"error": "instance already exists"}
    # pick a free IP on that provider
    code, resp = wing_request(p, "GET", "/api/agent/instances", timeout=20)
    used_ips = set()
    if code == 200 and isinstance(resp, dict):
        for i in resp.get("instances", []):
            if i.get("ip"):
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
        return 200, {"ok": True, "name": name, "ip": ip,
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
    except (TypeError, ValueError):
        return None, (400, {"error": "count must be 1-20"})
    if not (1 <= count <= FLEET_CREATE_MAX):
        return None, (400, {"error": "count must be 1-20"})
    try:
        cpus = float(cpus)
        memory_mb = int(memory_mb)
    except (TypeError, ValueError):
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


def fleet_headroom(instances, provider_id, count, memory_mb):
    """RAM/binder cost of adding `count` instances on one node.

    Returns a dict with a human-readable `warning` ("" when it fits).
    Best-effort: never raises, tolerates missing /proc or provider stats."""
    hr = {"host_ram_mb": 0, "current_limits_mb": 0, "new_limits_mb": 0,
          "overcommit_mb": 0, "binder_free_triplets": None,
          "binder_device_cap": FLEET_BINDER_DEVICE_CAP, "warning": ""}
    try:
        if provider_id == "local":
            hr["host_ram_mb"] = int(get_host_stats().get("ram_total_mb", 0) or 0)
            hr["current_limits_mb"] = sum(
                int(v.get("memory_mb", 0) or 0) for v in instances.values()
                if v.get("provider", "local") == "local")
            rc, out, _ = run("ls /dev/binderfs/ 2>/dev/null", timeout=10)
            if rc == 0:
                devs = [d for d in out.split()
                        if d == "binder" or re.fullmatch(r"binder\d+", d)]
                used = set()
                for v in instances.values():
                    for b in v.get("binders", []) or []:
                        used.add(b)
                hr["binder_free_triplets"] = max(
                    0, len([d for d in devs if d not in used]) // 3)
        else:
            st = load_providers().get(provider_id, {}).get("stats") or {}
            hr["host_ram_mb"] = int(st.get("ram_total_mb", 0) or 0)
            hr["current_limits_mb"] = sum(
                int(v.get("memory_mb", 0) or 0) for v in instances.values()
                if v.get("provider") == provider_id)
            # binder allocation on wing nodes is the agent's job
        hr["new_limits_mb"] = hr["current_limits_mb"] + count * memory_mb
        warns = []
        if hr["host_ram_mb"] > 0:
            over = hr["new_limits_mb"] - hr["host_ram_mb"]
            hr["overcommit_mb"] = max(0, over)
            if over > 0:
                warns.append(
                    f"memory limits would exceed host RAM by {over} MB "
                    f"({hr['new_limits_mb']} MB limits on {hr['host_ram_mb']} MB host). "
                    "Actual use is lower, but watch for OOM kills.")
        bf = hr["binder_free_triplets"]
        if bf is not None and count > bf:
            cap = FLEET_BINDER_DEVICE_CAP // 3
            warns.append(
                f"only {bf} binder triplet(s) free now; the pool self-heals "
                f"up to ~{cap} instances per host.")
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
                                  clean["memory_mb"])
    if not names:
        return 400, {"error": "all planned names already exist"}
    return 200, {"names": names, "total": len(names),
                 "prefix": clean["prefix"], "count": clean["count"],
                 "cpus": clean["cpus"], "memory_mb": clean["memory_mb"],
                 "provider": clean["provider"], "headroom": headroom}


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
    _sweep_batch_jobs()
    jid = secrets.token_hex(8)
    job = {"job_id": jid, "action": "create-fleet", "names": names,
           "prefix": clean["prefix"], "cpus": clean["cpus"],
           "memory_mb": clean["memory_mb"], "provider": clean["provider"],
           "total": len(names), "done": 0, "results": [],
           "status": "running", "created": time.time()}
    with _lock:
        _batch_jobs[jid] = job
    threading.Thread(target=_run_fleet_create, args=(jid,), daemon=True).start()
    return 202, {"job_id": jid, "action": "create-fleet",
                 "total": len(names), "names": names,
                 "provider": clean["provider"]}


def _run_fleet_create(jid):
    with _lock:
        job = _batch_jobs.get(jid)
        names = list(job["names"]) if job else []
        cpus = job["cpus"] if job else 2
        memory_mb = job["memory_mb"] if job else 2048
        provider = job["provider"] if job else "local"
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
    with _lock:
        job = _batch_jobs.get(jid)
        if job:
            job["status"] = "done"

# ---------------------------------------------------------------- HTTP handler

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class Handler(BaseHTTPRequestHandler):
    server_version = "DroidFarm/1.0"

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
        try:
            ln = int(self.headers.get("Content-Length", 0))
            if ln < 0 or ln > max_bytes:
                return {}
            return json.loads(self.rfile.read(ln) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return {}

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

        if path == "/" or path.startswith("/static/"):
            self._serve_static(path)
            return

        if path == "/api/host-stats":
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, get_host_stats())

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
            for pid, p in providers.items():
                # extract IP from agent URL
                ip = ""
                try:
                    ip = p.get("url", "").split("://")[1].split(":")[0].split("/")[0]
                except Exception:
                    pass
                out.append({
                    "id": pid, "name": p.get("name"), "hostname": p.get("hostname"),
                    "os": p.get("os"), "url": p.get("url"), "ip": ip,
                    "created": p.get("created"),
                    "last_seen": p.get("last_seen"), "stats": p.get("stats", {}),
                    "online": time.time() - p.get("last_seen", 0) < 120,
                    "version": p.get("version", ""),
                    "selfwatch": p.get("selfwatch", {}),
                })
            # include local as a pseudo-provider
            ls = get_host_stats()
            out.insert(0, {"id": "local", "name": "Main node (local)",
                           "hostname": "main", "os": "Linux", "url": "local",
                           "last_seen": time.time(), "stats": ls, "online": True,
                           "version": "local"})
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
            # fetch remote statuses in one go per provider
            remote_status = {}
            for pid, p in providers.items():
                if time.time() - p.get("last_seen", 0) > 120:
                    continue
                code, resp = wing_request(p, "GET", "/api/agent/instances", timeout=5)
                if code == 200 and isinstance(resp, dict):
                    for i in resp.get("instances", []):
                        remote_status[(pid, i["name"])] = i
            login_results = load_login_results()
            out = []
            for name, inst in instances.items():
                prov = inst.get("provider", "local")
                pname = "Main node" if prov == "local" else providers.get(prov, {}).get("name", prov)
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
                        "ip": inst.get("ip"), "cpus": inst.get("cpus"),
                        "memory_mb": inst.get("memory_mb"),
                        "instance_id": inst.get("instance_id"),
                        "created_at": inst.get("created_at"),
                        # watchdog only runs on the main node; remote health unknown
                        "degraded": False, "degraded_reason": None,
                        "login_ok": lr.get("ok") if lr else None,
                        "login_ts": lr.get("ts") if lr else None,
                        "login_by": lr.get("by") if lr else None,
                        # live usage: reported by the node agent (>=1.0.4);
                        # older agents omit the fields -> None (same as "no data")
                        "mem_used_mb": clean_agent_resource(rs.get("mem_used_mb")),
                        "cpu_pct": clean_agent_resource(rs.get("cpu_pct")),
                    })
                    continue
                cname = f"wd-{name}"
                status = container_status(cname)
                # Fast setup status via marker files (no slow waydroid exec)
                vol_base = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/droidfarm"
                booted = os.path.exists(os.path.join(vol_base, "booted"))
                roblox_done = os.path.exists(os.path.join(vol_base, "roblox_done"))
                if status == "running" and booted and roblox_done:
                    setup = "ready"
                elif status == "running" and booted:
                    setup = "installing_roblox"
                elif status == "running":
                    setup = "booting"
                else:
                    setup = status  # stopped, etc.
                health = instance_health(name)
                lr = login_results.get(name)
                lr = lr if isinstance(lr, dict) else None
                res = instance_resources(name)
                out.append({
                    "name": name,
                    "status": status,
                    "setup": setup,
                    "provider": "local",
                    "provider_name": "Main node",
                    "ip": inst.get("ip") or container_ip(cname),
                    "cpus": inst.get("cpus"),
                    "memory_mb": inst.get("memory_mb"),
                    "instance_id": inst.get("instance_id"),
                    "created_at": inst.get("created_at"),
                    "degraded": health["degraded"],
                    "degraded_reason": health["reason"],
                    "login_ok": lr.get("ok") if lr else None,
                    "login_ts": lr.get("ts") if lr else None,
                    "login_by": lr.get("by") if lr else None,
                    # live usage (None until the poller has data / stale entries)
                    "mem_used_mb": res["mem_used_mb"] if res else None,
                    "cpu_pct": res["cpu_pct"] if res else None,
                })
            return self._json(200, {"instances": out,
                                    "fleet_resources": fleet_resource_summary()})

        m = re.fullmatch(r"/api/instances/([^/]+)/roblox", path)
        if m:
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            name = unquote(m.group(1))
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
            accounts = load_accounts()
            acct = accounts.get(name, {})
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
            safe = re.sub(r"[^A-Za-z0-9_-]", "", name)
            lr = load_login_results().get(safe)
            return self._json(200, {"result": lr if isinstance(lr, dict) else None})

        self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/login":
            ip = self.client_address[0]
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
            ip = self.client_address[0]
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
            providers[pid] = {
                "id": pid, "name": str(data.get("hostname", host))[:40],
                "hostname": str(data.get("hostname", ""))[:64],
                "os": str(data.get("os", ""))[:16],
                "machine_id": machine_id,
                "url": agent_url, "agent_key": agent_key,
                "created": providers.get(pid, {}).get("created", time.time()),
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
            p = providers.get(pid)
            if not p or not hmac.compare_digest(str(data.get("agent_key", "")), p["agent_key"]):
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
                except (TypeError, ValueError):
                    rec = 0
                p["selfwatch"] = {"recoveries": rec, "degraded": clean_deg,
                                  "updated": time.time()}
            inst = data.get("instances", [])
            if isinstance(inst, list):
                p["instances"] = [str(x)[:32] for x in inst[:50] if isinstance(x, str)]
            try:
                rport = int(data.get("port", 0))
                if rport and 1 <= rport <= 65535:
                    base = p.get("url", "")
                    p["url"] = re.sub(r":\d+$", f":{rport}", base) or base
            except (TypeError, ValueError):
                pass
            save_providers(providers)
            return self._json(200, {"ok": True})

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

        m = re.fullmatch(r"/api/instances/([^/]+)/(start|stop)", path)
        if m:
            code, resp = instance_single_action(unquote(m.group(1)), m.group(2))
            return self._json(code, resp)

        m = re.fullmatch(r"/api/instances/([^/]+)/input", path)
        if m:
            return self._handle_input(unquote(m.group(1)))

        m = re.fullmatch(r"/api/instances/([^/]+)/webrtc/offer", path)
        if m:
            return self._handle_webrtc_offer(unquote(m.group(1)))

        m = re.fullmatch(r"/api/instances/([^/]+)/launch-roblox", path)
        if m:
            code, resp = instance_single_action(unquote(m.group(1)), "launch-roblox")
            return self._json(code, resp)

        # Roblox account credentials (per-instance auto-login) - POST only (GET is in do_GET)
        m = re.fullmatch(r"/api/instances/([^/]+)/roblox-account", path)
        if m:
            name = unquote(m.group(1))
            try:
                body = json.loads(self._read_body() or "{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})
            accounts = load_accounts()
            acct = accounts.get(name, {})
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
            prov = load_instances().get(name, {}).get("provider", "local")
            if prov == "local":
                cname = f"wd-{name}"
                if container_status(cname) != "running":
                    return self._json(409, {"error": "instance not running"})
            else:
                p = load_providers().get(prov)
                if not p:
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
            owned = [n for n, v in load_instances().items() if v.get("provider") == pid]
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
        with _lock:
            instances = load_instances()
            prov = instances.get(name, {}).get("provider", "local")
        # remote provider: proxy delete
        if prov != "local":
            providers = load_providers()
            p = providers.get(prov)
            if not p:
                return self._json(404, {"error": "node not found"})
            code, resp = wing_request(p, "DELETE", f"/api/agent/instances/{name}", timeout=60)
            if code == 200:
                with _lock:
                    instances = load_instances()
                    instances.pop(name, None)
                    save_instances(instances)
            return self._json(code, resp if isinstance(resp, dict) else {"ok": code == 200})
        cname = f"wd-{name}"
        run(f"docker stop {cname}", timeout=60)
        run(f"docker rm -f {cname}", timeout=30)
        run(f"docker volume rm {cname}-varlib {cname}-data", timeout=30)
        with _lock:
            instances = load_instances()
            instances.pop(name, None)
            save_instances(instances)
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
        except (TypeError, ValueError):
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
        prov = load_instances().get(name, {}).get("provider", "local")
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
        prov = load_instances().get(name, {}).get("provider", "local")
        if prov != "local":
            p = load_providers().get(prov)
            if not p:
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
        if not fpath.startswith(STATIC_DIR) or not os.path.isfile(fpath):
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
        if not self._authed_ws():
            self._send(401, b"unauthorized")
            return
        prov = load_instances().get(name, {}).get("provider", "local")
        wing = load_providers().get(prov) if prov != "local" else None
        if prov != "local" and not wing:
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
                if op is None or op == 0x8:  # close/error
                    stop.set()
                    break

        threading.Thread(target=reader, daemon=True).start()
        # Ensure the screencap script exists (one-time setup per container)
        if prov == "local":
            ensure_cap_script(name)

        def get_frame():
            if wing:
                code, data = wing_request(wing, "GET",
                                         f"/api/agent/instances/{name}/screenshot",
                                         timeout=10)
                if code == 200 and isinstance(data, bytes) and len(data) > 1000:
                    return data
                return None
            return capture_screenshot(name)

        try:
            while not stop.is_set():
                t0 = time.time()
                frame = get_frame()
                if frame:
                    try:
                        ws_send_binary(sock, frame)
                    except OSError:
                        break
                # Target ~20 fps (0.05s per frame); actual rate limited by screencap speed
                dt = time.time() - t0
                if dt < 0.05:
                    stop.wait(0.05 - dt)
        finally:
            stop.set()
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
        if not HAS_H264:
            self._send(501, b"h264 unavailable (ffmpeg or PIL missing)")
            return
        if not self._authed_ws():
            self._send(401, b"unauthorized")
            return
        prov = load_instances().get(name, {}).get("provider", "local")
        wing = load_providers().get(prov) if prov != "local" else None
        if prov != "local" and not wing:
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


def main():
    load_tokens()
    if PASSWORD == "changeme":
        print("WARNING: using default password 'changeme'. Set DROIDFARM_PASSWORD!")
    webrtc_init()
    threading.Thread(target=auto_login_watcher, daemon=True,
                     name="auto-login-watcher").start()
    threading.Thread(target=resource_poller, daemon=True,
                     name="resource-poller").start()
    print(f"DroidFarm manager on :{PORT} (PIL: {HAS_PIL}, WebRTC: {HAS_AIORTC}, H.264: {HAS_H264})")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
