#!/usr/bin/env python3
"""Jerusalem AI screen-touch agent (nano-gpt, GLM 5.3 Uncensored, vision).

What it does, in one loop:
  1. Screenshot the Waydroid instance (the same live.png frame the
     dashboard viewer shows, downscaled to a small JPEG).
  2. Send that frame + a short goal to nano-gpt (vision model) with a
     system prompt that explains the touch system.
  3. The model replies with ONE strict-JSON action (tap / swipe / key /
     wait / done / need_help) in per-mille screen coordinates.
  4. The agent executes it through the Jerusalem manager input API
     (POST /api/instances/<name>/input, Bearer token from /api/login).
  5. Repeat until done, or until it gets stuck — then it STOPS and
     hands the screen to you on your phone (see PHONE TAKEOVER below).

Token optimization (why this stays cheap):
  * Small frames: 480px wide JPEG quality 60 (~6-30 KB, same as the
    dashboard viewer) instead of full 1024x608 PNGs.
  * Stateless calls: no growing chat history. Each call carries only the
    system prompt, the current frame, the goal, and the previous action.
  * Send-on-change: if the frame bytes are identical to last step, the
    API is NOT called at all (static loading/boot screens cost $0).
  * Short output: max_tokens 200, temperature 0.2, JSON-only replies —
    no billed reasoning prose.
  * Verified pricing for the default model (2026-10-28, nano-gpt):
    $0.20 / 1M input tokens, $0.80 / 1M output tokens. A typical step
    (~1k image tokens + ~300 prompt tokens + ~50 output tokens) costs
    roughly $0.0003. A hundred steps is a few cents.

PHONE TAKEOVER ("use the phone in case of problems"):
  The agent NEVER taps blindly. It stops touching and writes a takeover
  flag file when any of these happen:
    * the model replies need_help,
    * 3 invalid replies in a row,
    * 3 consecutive input-API failures,
    * 10 steps with no screen change at all (wedged game / stuck menu).
  On takeover it prints exactly what happened + the last step, and exits.
  You then open the dashboard on your phone and drive the instance
  yourself. Restart the agent when you want it to resume.

Safety:
  * Config comes ONLY from environment variables. This script never
    writes any key or password to disk, logs, or output.
  * The model never sees any password. Text input is limited to the
    manager's own sanitizer (letters/digits/basic punctuation, 200 chars).
  * The agent never logs into Roblox itself — the instance must already
    be logged in (or use Jerusalem's existing auto-login). It only
    touches the screen toward the GOAL you set.
  * --dry-run logs the actions it WOULD take without sending any input.

Usage (on the VPS host, where the docker volumes live):
  export NANO_GPT_API_KEY="..."            # nano-gpt key, env only
  export JERUSALEM_DASHBOARD_PASSWORD="..." # dashboard login password
  export INSTANCE="srt1"
  export GOAL="Open Roblox and join the game Bloxburg"
  python3 ai_player.py --once      # one step, for testing
  python3 ai_player.py             # full loop (default max 300 steps)
  python3 ai_player.py --dry-run   # see actions without touching
  python3 ai_player.py --selftest  # offline checks, no key needed

Environment:
  NANO_GPT_API_KEY            required (nano-gpt Bearer key)
  NANO_GPT_MODEL              default: z-ai/glm-5.3-flash-uncensored
                              (verified 2026-10-28: vision=true,
                              text+image->text, 1M context)
  NANO_GPT_BASE_URL           default: https://nano-gpt.com/api/v1
  JERUSALEM_URL               default: http://127.0.0.1:8080
  JERUSALEM_DASHBOARD_PASSWORD required (unless --dry-run)
  JERUSALEM_TOKEN           (minted by the dashboard when it spawns the
                             agent; wins over the password login)
  AI_STATUS_FILE            (dashboard status JSON the agent updates
                             every step; empty = no status file)
  AI_LOG_FILE               (dashboard console log, JSONL: one entry per
                             AI request/response; metadata only, never
                             image bytes, keys, or passwords)
  INSTANCE                    instance name, e.g. srt1
  GOAL                        what the agent should do in Roblox
  POLL_SECS                   default: 3
  MAX_STEPS                   default: 300
  SHOT_PATH                   override screenshot file (testing)
  TAKEOVER_FILE               default: ./ai-takeover.json
"""

import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.request

try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

BASE_URL = os.environ.get("NANO_GPT_BASE_URL", "https://nano-gpt.com/api/v1")
MODEL = os.environ.get("NANO_GPT_MODEL", "z-ai/glm-5.3-flash-uncensored")
JERUSALEM_URL = os.environ.get("JERUSALEM_URL", "http://127.0.0.1:8080")
TAKEOVER_FILE = os.environ.get("TAKEOVER_FILE", "./ai-takeover.json")
STATUS_FILE = os.environ.get("AI_STATUS_FILE", "")
_META = {}  # instance + goal, filled by main(), reported in the status file

# Verified 2026-10-28 for the default model (nano-gpt model catalog).
PRICE_IN_PER_M = 0.20
PRICE_OUT_PER_M = 0.80

ALLOWED_KEYS = {"HOME", "BACK", "APP_SWITCH", "POWER", "VOLUME_UP", "VOLUME_DOWN"}

SYSTEM_PROMPT = """You are driving an Android phone screen inside a cloud gaming PC. You see screenshots of the screen. You can touch it. That is the whole system.

Screen: 1024 wide x 608 tall, but you answer in per-mille coordinates: x 0 (left edge) to 1000 (right edge), y 0 (top edge) to 1000 (bottom edge). A tap at x=500, y=500 is the center. Never answer in pixels.

Actions you may output, and ONLY these, as strict JSON with no other text:
{"action": "tap", "x": 500, "y": 300, "note": "tap Play button"}
{"action": "swipe", "x1": 500, "y1": 800, "x2": 500, "y2": 300, "note": "scroll list up"}
{"action": "key", "key": "BACK", "note": "go back"}  (keys: HOME, BACK, APP_SWITCH, POWER, VOLUME_UP, VOLUME_DOWN)
{"action": "wait", "note": "loading screen, wait"}
{"action": "done", "note": "goal reached"}
{"action": "need_help", "note": "stuck on login screen"}

Rules:
- Look at the CURRENT screenshot only. Tap what is actually visible, never guess a button that is not there.
- If the screen is loading or unchanged, answer wait, not a random tap.
- If you are stuck, looping, or asked to log in / pay / enter a password, answer need_help. Never enter credentials.
- Keep note to 10 words or fewer. Output JSON only, no explanation.
"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def shot_path(instance):
    if os.environ.get("SHOT_PATH"):
        return os.environ["SHOT_PATH"]
    cname = f"wd-{instance}"
    base = f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0"
    for suffix in ("jerusalem/live.png", "droidfarm/live.png"):
        p = os.path.join(base, suffix)
        if os.path.exists(p):
            return p
    return os.path.join(base, "jerusalem/live.png")


def read_frame(instance):
    """Return small JPEG bytes of the current screen, or None."""
    p = shot_path(instance)
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    if HAS_PIL:
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            if img.width > 480:
                h = int(img.height * 480 / img.width)
                img = img.resize((480, h), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=60, optimize=True)
            return buf.getvalue()
        except Exception:
            return raw
    return raw


def ask_model(frame_jpeg, goal, last_action):
    """One vision call. Returns (action_dict_or_None, usage_dict)."""
    api_key = os.environ.get("NANO_GPT_API_KEY", "")
    if not api_key:
        raise RuntimeError("NANO_GPT_API_KEY is not set")
    b64 = base64.b64encode(frame_jpeg).decode()
    body = {
        "model": MODEL,
        "temperature": 0.2,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text",
                 "text": f"Goal: {goal}\nPrevious action: {last_action or 'none'}\nWhat is on screen now, and what is the single next action? JSON only."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ],
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        out = json.load(resp)
    content = out["choices"][0]["message"]["content"]
    usage = out.get("usage", {})
    try:
        action = json.loads(content)
    except Exception:
        return None, usage
    return action, usage


def validate_action(action):
    """Return a clean action dict, or None if invalid."""
    if not isinstance(action, dict):
        return None
    kind = action.get("action")
    note = str(action.get("note", ""))[:80]
    if kind == "tap":
        try:
            x, y = int(action["x"]), int(action["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            return None
        return {"type": "tap", "x": x, "y": y, "note": note}
    if kind == "swipe":
        try:
            x1, y1 = int(action["x1"]), int(action["y1"])
            x2, y2 = int(action["x2"]), int(action["y2"])
        except (KeyError, TypeError, ValueError):
            return None
        if not all(0 <= v <= 1000 for v in (x1, y1, x2, y2)):
            return None
        return {"type": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "note": note}
    if kind == "key":
        key = str(action.get("key", "")).upper()
        if key not in ALLOWED_KEYS:
            return None
        return {"type": "key", "key": key, "note": note}
    if kind in ("wait", "done", "need_help"):
        return {"type": kind, "note": note}
    return None


def manager_login():
    """Bearer token for the manager input API. Raises on failure.

    JERUSALEM_TOKEN (minted by the dashboard when it spawns the agent)
    wins; otherwise POST /api/login with the dashboard password.
    """
    preset = os.environ.get("JERUSALEM_TOKEN", "")
    if preset:
        return preset
    password = os.environ.get("JERUSALEM_DASHBOARD_PASSWORD", "")
    if not password:
        raise RuntimeError("JERUSALEM_DASHBOARD_PASSWORD is not set")
    req = urllib.request.Request(
        f"{JERUSALEM_URL}/api/login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.load(resp)
    token = out.get("token", "")
    if not token:
        raise RuntimeError("dashboard login failed (no token)")
    return token


def manager_input(token, instance, action):
    """POST one input action. Returns (ok, response_dict)."""
    req = urllib.request.Request(
        f"{JERUSALEM_URL}/api/instances/{instance}/input",
        data=json.dumps(action).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return False, json.load(e)
        except Exception:
            return False, {"error": f"http {e.code}"}
    except Exception as e:
        return False, {"error": str(e)[:120]}


def write_status(state, step=0, last_action="", tokens_in=None,
                 tokens_out=None, cost=None, takeover=""):
    """Update the dashboard status file. Best-effort, never raises.

    Merges with the previous content so fields the caller does not pass
    (pid, instance, goal, earlier token counts) survive. No secrets are
    ever written here — only step counts, notes, and cost.
    """
    if not STATUS_FILE:
        return
    try:
        try:
            with open(STATUS_FILE) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.update(_META)
        data.update({"state": state, "step": step, "last_action": last_action,
                     "pid": os.getpid(), "at": int(time.time())})
        if tokens_in is not None:
            data["tokens_in"] = tokens_in
        if tokens_out is not None:
            data["tokens_out"] = tokens_out
        if cost is not None:
            data["cost"] = cost
        if takeover:
            data["takeover"] = takeover
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass


def write_takeover(instance, reason, step, last_action):
    data = {"instance": instance, "reason": reason, "step": step,
            "last_action": last_action, "at": int(time.time()),
            "message": "AI stopped. Open the Jerusalem dashboard on your phone "
                       "and drive this instance yourself."}
    with open(TAKEOVER_FILE, "w") as f:
        json.dump(data, f, indent=2)
    write_status("takeover", step=step, last_action=last_action, takeover=reason)
    return data


def log_call(entry):
    """Append one console-log entry (JSONL). Best-effort, never raises.

    The log holds request/response metadata only — never image bytes,
    never keys, never passwords — so the dashboard console stays small
    and safe to show. Oldest entries are dropped past 2000 lines.
    """
    path = os.environ.get("AI_LOG_FILE", "")
    if not path:
        return
    try:
        entry = dict(entry)
        entry["at"] = int(time.time())
        entry["ts"] = time.strftime("%H:%M:%S")
        line = json.dumps(entry)
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            lines = []
        if len(lines) > 2000:
            lines = lines[-1000:]
        lines.append(line + "\n")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(lines)
        os.replace(tmp, path)
    except Exception:
        pass


def selftest():
    """Offline checks: no key, no network, no instance needed."""
    checks = []
    checks.append(("tap valid", validate_action(
        {"action": "tap", "x": 500, "y": 300, "note": "tap Play"}) is not None))
    checks.append(("tap out of range rejected", validate_action(
        {"action": "tap", "x": 1500, "y": 300}) is None))
    checks.append(("swipe valid", validate_action(
        {"action": "swipe", "x1": 500, "y1": 800, "x2": 500, "y2": 300}) is not None))
    checks.append(("key valid", validate_action(
        {"action": "key", "key": "BACK"}) is not None))
    checks.append(("bad key rejected", validate_action(
        {"action": "key", "key": "ENTER"}) is None))
    checks.append(("need_help valid", validate_action(
        {"action": "need_help", "note": "stuck"}) is not None))
    checks.append(("garbage rejected", validate_action(
        {"action": "dance"}) is None))
    checks.append(("prompt mentions per-mille", "per-mille" in SYSTEM_PROMPT))
    checks.append(("prompt forbids credentials", "Never enter credentials" in SYSTEM_PROMPT))
    ok = True
    for name, passed in checks:
        print(("PASS " if passed else "FAIL ") + name)
        ok = ok and passed
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Jerusalem AI screen-touch agent")
    ap.add_argument("--instance", default=os.environ.get("INSTANCE", ""))
    ap.add_argument("--goal", default=os.environ.get("GOAL", ""))
    ap.add_argument("--max-steps", type=int,
                    default=int(os.environ.get("MAX_STEPS", "300")))
    ap.add_argument("--poll-secs", type=float,
                    default=float(os.environ.get("POLL_SECS", "3")))
    ap.add_argument("--once", action="store_true", help="run a single step")
    ap.add_argument("--dry-run", action="store_true",
                    help="log actions without sending input")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.instance or not args.goal:
        ap.error("--instance and --goal (or INSTANCE / GOAL env) are required")

    token = None
    if not args.dry_run:
        token = manager_login()
        log("dashboard login ok")
    _META.update({"instance": args.instance, "goal": args.goal})
    write_status("running", step=0)

    last_frame = None
    last_action = None
    bad_replies = 0
    failed_inputs = 0
    no_change_steps = 0
    total_in = total_out = 0

    for step in range(1, args.max_steps + 1):
        frame = read_frame(args.instance)
        if frame is None:
            log(f"step {step}: no screenshot yet, waiting")
            time.sleep(args.poll_secs)
            continue
        if frame == last_frame:
            no_change_steps += 1
            if no_change_steps >= 10:
                data = write_takeover(args.instance,
                                      "screen unchanged for 10 steps (wedged?)",
                                      step, last_action)
                log(f"TAKEOVER: {data['reason']}. {data['message']}")
                return 2
            time.sleep(args.poll_secs)
            continue
        no_change_steps = 0
        last_frame = frame

        log_call({"type": "request", "step": step, "model": MODEL,
                  "goal": args.goal, "prev": last_action or "",
                  "frame_bytes": len(frame)})
        try:
            raw, usage = ask_model(frame, args.goal, last_action)
        except Exception as e:
            log(f"step {step}: model call failed: {str(e)[:120]}")
            log_call({"type": "error", "step": step, "error": str(e)[:120]})
            time.sleep(args.poll_secs)
            continue
        log_call({"type": "response", "step": step, "raw": raw,
                  "tokens_in": usage.get("prompt_tokens", 0),
                  "tokens_out": usage.get("completion_tokens", 0)})
        total_in += usage.get("prompt_tokens", 0)
        total_out += usage.get("completion_tokens", 0)
        cost = total_in / 1_000_000 * PRICE_IN_PER_M + total_out / 1_000_000 * PRICE_OUT_PER_M

        action = validate_action(raw)
        if action is None:
            bad_replies += 1
            log(f"step {step}: invalid model reply ({bad_replies}/3)")
            if bad_replies >= 3:
                data = write_takeover(args.instance,
                                      "3 invalid model replies in a row",
                                      step, last_action)
                log(f"TAKEOVER: {data['reason']}. {data['message']}")
                return 2
            time.sleep(args.poll_secs)
            continue
        bad_replies = 0

        kind = action["type"]
        log(f"step {step}: {kind} {action.get('note','')} "
            f"(tokens in={total_in} out={total_out} ~${cost:.4f})")
        write_status("running", step=step, last_action=action.get("note", ""),
                     tokens_in=total_in, tokens_out=total_out, cost=round(cost, 4))
        if kind == "done":
            log("goal reached, stopping")
            write_status("done", step=step, last_action=action.get("note", ""),
                         tokens_in=total_in, tokens_out=total_out, cost=round(cost, 4))
            return 0
        if kind == "need_help":
            data = write_takeover(args.instance,
                                  f"model asked for help: {action.get('note','')}",
                                  step, last_action)
            log(f"TAKEOVER: {data['reason']}. {data['message']}")
            return 2
        if kind == "wait":
            last_action = "wait"
            time.sleep(args.poll_secs)
            continue

        last_action = json.dumps(action)
        if args.dry_run:
            log(f"dry-run: would send {action}")
        else:
            ok, resp = manager_input(token, args.instance, action)
            if not ok:
                failed_inputs += 1
                log(f"input failed ({failed_inputs}/3): {resp}")
                log_call({"type": "execute", "step": step, "action": action, "ok": False})
                if failed_inputs >= 3:
                    data = write_takeover(args.instance,
                                          "3 consecutive input failures",
                                          step, last_action)
                    log(f"TAKEOVER: {data['reason']}. {data['message']}")
                    return 2
            else:
                failed_inputs = 0
                log_call({"type": "execute", "step": step, "action": action, "ok": True})
        if args.once:
            return 0
        time.sleep(args.poll_secs)

    log("max steps reached, stopping")
    write_status("stopped", step=args.max_steps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
