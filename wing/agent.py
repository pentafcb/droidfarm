#!/usr/bin/env python3
import base64
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

VERSION = "1.0.4"
IS_WINDOWS = platform.system() == "Windows"

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


def create_instance_linux(name, cpus, memory_mb, ip):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        return False, "invalid name"
    cname = f"wd-{name}"
    st = container_status(cname)
    if st == "unknown":
        return False, "docker unavailable"
    if st != "missing":
        return False, "already exists"
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
    # find binder devices (binderfs may have 'binder' or 'binder0' naming)
    binder_devs = []
    for cand in ["/dev/binderfs/binder", "/dev/binderfs/binder0"]:
        if os.path.exists(cand):
            base = cand
            break
    else:
        base = None
    if base:
        # determine the numbering scheme
        if base.endswith("binder0"):
            devs = ["/dev/binderfs/binder0", "/dev/binderfs/binder1", "/dev/binderfs/binder2"]
        else:
            devs = ["/dev/binderfs/binder", "/dev/binderfs/binder1", "/dev/binderfs/binder2"]
        for d in devs:
            if os.path.exists(d):
                binder_devs.append(d)
    if len(binder_devs) < 3:
        return False, f"binder devices not found (found {len(binder_devs)})"
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
    cname = f"wd-{name}"
    docker("rm", "-f", cname, timeout=60)
    docker("volume", "rm", f"{cname}-data", timeout=30)
    docker("volume", "rm", f"{cname}-varlib", timeout=30)
    inst = load_instances()
    inst.pop(name, None)
    save_instances(inst)
    return True


def instance_status_linux(name):
    cname = f"wd-{name}"
    st = container_status(cname)
    if st != "running":
        return {"name": name, "running": False, "setup": "stopped", "roblox": False,
                "mem_used_mb": None, "cpu_pct": None}
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
        "data/media/0/droidfarm/booted",
        "data/booted",
        "booted",
    ])
    roblox_done = any(os.path.exists(os.path.join(base, p)) for p in [
        "data/media/0/droidfarm/roblox_done",
        "data/roblox_done",
        "roblox_done",
    ])
    setup = "ready" if (booted and roblox_done) else ("installing_roblox" if booted else "booting")
    res = instance_resources_all().get(name, {})
    return {"name": name, "running": True, "setup": setup, "roblox": roblox_done,
            "started_at": started_at,
            "mem_used_mb": res.get("mem_used_mb"), "cpu_pct": res.get("cpu_pct")}


def sanitize_input_text(raw):
    """Make a string safe for `input text` through the input FIFO (which is
    eval'd by sh). Mirrors the manager's local sanitize: spaces -> %s
    (Android's input-text space encoding), everything shell-dangerous stripped."""
    txt = str(raw).replace(" ", "%s")
    txt = re.sub(r"[^a-zA-Z0-9%s.,!?@_-]", "", txt)
    return txt[:200]


_shot_cache = {}  # name -> (mtime, jpeg_bytes)

def screenshot_bytes_linux(name):
    cname = f"wd-{name}"
    base = f"/var/lib/docker/volumes/{cname}-data/_data"
    p = None
    for rel in ["data/media/0/droidfarm/live.png", "data/live.png", "live.png"]:
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
    for rel in ["data/media/0/droidfarm/input.fifo", "data/input.fifo", "input.fifo"]:
        p = os.path.join(base, rel)
        if os.path.exists(p):
            return p
    return os.path.join(base, "data/media/0/droidfarm/input.fifo")


def input_linux(name, cmd):
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


def launch_roblox_linux(name):
    cmds = ["input keyevent 3", "sleep 1", "input swipe 512 400 512 100",
            "sleep 1", "input tap 129 424"]
    cname = f"wd-{name}"
    fifo = _find_fifo(cname)
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            for c in cmds:
                os.write(fd, (c + "\n").encode())
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
        return self.headers.get("X-Agent-Key", "") == AGENT_KEY and AGENT_KEY

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authed():
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
        if path == "/api/agent/instances":
            if not require_linux():
                return self._send(200, {"instances": []})
            inst = load_instances()
            out = []
            for name, meta in inst.items():
                st = instance_status_linux(name)
                st.update(meta)
                out.append(st)
            return self._send(200, {"instances": out})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/screenshot", path)
        if m:
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            data = screenshot_bytes_linux(unquote(m.group(1)))
            if data is None:
                return self._send(404, {"error": "no screenshot"})
            return self._send_bytes(200, data, "image/png")
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/status", path)
        if m:
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            return self._send(200, instance_status_linux(unquote(m.group(1))))
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/logs", path)
        if m:
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            name = unquote(m.group(1))
            cname = f"wd-{name}"
            rc, out = docker("logs", "--tail", "50", cname, timeout=10)
            return self._send(200, {"logs": out[-4000:]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authed():
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
            except (TypeError, ValueError):
                return self._send(400, {"error": "invalid cpus/memory"})
            ok, msg = create_instance_linux(name, cpus, memory_mb, ip)
            return self._send(200 if ok else 500, {"ok": ok, "msg": msg})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/start", path)
        if m:
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            name = unquote(m.group(1))
            cname = f"wd-{name}"
            rc, out = docker("start", cname, timeout=30)
            return self._send(200 if rc == 0 else 500, {"ok": rc == 0, "msg": out[-500:]})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/input", path)
        if m:
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            data = self._read_json()
            itype = data.get("type")
            cmd = None
            if itype == "tap":
                try:
                    x = int(float(data.get("x")) / 1000 * DISP_W)
                    y = int(float(data.get("y")) / 1000 * DISP_H)
                    cmd = f"input tap {x} {y}"
                except (TypeError, ValueError):
                    pass
            elif itype == "key":
                keys = {"HOME": 3, "BACK": 4, "APP_SWITCH": 187}
                code = keys.get(str(data.get("key", "")).upper())
                if code is not None:
                    cmd = f"input keyevent {code}"
            elif itype == "text":
                # Text entry (e.g. Roblox login fields). Send {"text": "..."}
                # or {"text": "<base64>", "b64": true} when the string has
                # shell-special characters. Sanitized before eval.
                raw = data.get("text", "")
                if data.get("b64"):
                    try:
                        raw = base64.b64decode(str(raw)).decode(errors="replace")
                    except Exception:
                        raw = ""
                txt = sanitize_input_text(raw)
                if txt:
                    cmd = f"input text {txt}"
            if not cmd:
                return self._send(400, {"error": "invalid input"})
            ok = input_linux(unquote(m.group(1)), cmd)
            return self._send(200 if ok else 500, {"ok": ok})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/launch-roblox", path)
        if m:
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            ok = launch_roblox_linux(unquote(m.group(1)))
            return self._send(200 if ok else 500, {"ok": ok})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)/(start|stop)", path)
        if m:
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            name, action = unquote(m.group(1)), m.group(2)
            cname = f"wd-{name}"
            rc, _ = docker("start" if action == "start" else "stop", cname, timeout=60)
            return self._send(200 if rc == 0 else 500, {"ok": rc == 0})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        m = re.fullmatch(r"/api/agent/instances/([^/]+)", path)
        if m:
            if not require_linux():
                return self._send(501, {"error": "android runtime not available on this OS"})
            delete_instance_linux(unquote(m.group(1)))
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
_SELF_WATCH_DF = "/data/media/0/droidfarm"


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


def selfwatch_check_instance(name, st_dir=None, vol_base="/var/lib/docker/volumes"):
    """One recovery pass for a node instance. Returns True when all green.

    st_dir / vol_base are injectable for tests; production defaults apply.
    """
    if st_dir is None:
        st_dir = SELF_WATCH_DIR
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        return True
    cname = "wd-" + name
    healthy = True
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
    # 2. helper daemons alive? (screencap loop, input daemon, keepalive)
    for script in _SELF_WATCH_HELPERS:
        rc, _ = docker("exec", cname, "pgrep", "-f", script, timeout=15)
        if rc != 0:
            healthy = False
            key = name + ".helper-" + re.sub(r"[^a-zA-Z0-9]", "_", script)
            ev = _sw_cb_guard(st_dir, name, key, 600, 8, script)
            if ev is not None:
                rc2, out2 = docker("exec", "-d", cname, "sh", "-c",
                                   "nohup waydroid shell sh %s/%s >/dev/null 2>&1"
                                   % (_SELF_WATCH_DF, script), timeout=15)
                if rc2 == 0:
                    _sw_log(st_dir, "%s %s dead -> restart (%d/8 per 10m)" % (name, script, ev))
                    _sw_bump_recoveries(st_dir)
                else:
                    _sw_log(st_dir, "%s %s restart FAILED: %s" % (name, script, (out2 or "")[-120:]))
    # 3. screencap fresh? (stale >30s -> kill + restart loop; cb 10/hour)
    png = os.path.join(vol_base, cname + "-data", "_data",
                       "data", "media", "0", "droidfarm", "live.png")
    try:
        age = time.time() - os.path.getmtime(png)
    except Exception:
        age = -1
    if age >= 30:
        healthy = False
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
    # 4. degraded auto-clear: all checks green -> resume auto-recovery
    marker = os.path.join(st_dir, name + ".degraded")
    if healthy and os.path.exists(marker):
        for fn in os.listdir(st_dir):
            if fn.startswith(name + ".") and (fn.endswith(".ev") or fn.endswith(".degraded")):
                try:
                    os.remove(os.path.join(st_dir, fn))
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


def selfwatch_loop():
    if IS_WINDOWS:
        return
    while True:
        try:
            for name in list(load_instances().keys()):
                try:
                    selfwatch_check_instance(name)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(SELF_WATCH_INTERVAL)


def build_heartbeat_payload():
    stats = get_host_stats()
    payload = {"provider_id": PROVIDER_ID, "agent_key": AGENT_KEY,
               "stats": stats, "version": VERSION,
               "port": BOUND_PORT,
               "instances": list(load_instances().keys())}
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
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass
        time.sleep(30)


BOUND_PORT = PORT


def main():
    global BOUND_PORT
    t = threading.Thread(target=heartbeat_loop, daemon=True)
    t.start()
    t2 = threading.Thread(target=selfwatch_loop, daemon=True)
    t2.start()
    port = PORT
    srv = None
    for _ in range(10):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError:
            port += 1
    if srv is None:
        time.sleep(30)
        return
    BOUND_PORT = port
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
