#!/usr/bin/env python3
"""Tests for backup-visibility (pending-deploy/backup-visibility).

GAP: the 03:17 daily state backup ran from cron with zero dashboard
visibility — a failed or never-firing backup was invisible until a
restore was needed. backup-state.sh now writes $DATA/last-backup.json
on every run (success and failure); manager/app.py reads it via
read_backup_status() + an auth-gated GET /api/backup-status; the
dashboard fleet bar renders it (green "hace Xh (Y MB)" / red "falló" /
dim "sin datos"); verify-live.sh --ssh probes it WARN-only.

Covers (hermetic):
  A. read_backup_status units (importlib-loaded real app.py,
     BACKUP_STATUS_FILE monkeypatched to temp): missing/corrupt/
     non-dict markers -> known False; ok:true/false shapes; garbage
     field types coerced; error truncated to 160; never raises.
  B. Route contract: /api/backup-status inside do_GET, auth gate before
     the read, 401 shape.
  C. Dashboard: the marked backup-line block extracted and driven in
     node with stubbed $/api — render matrix, 5-min cache, age strings,
     failure keeps old text.
  D. Dashboard contract: #backupLine span placement, refresh() wiring,
     Spanish labels, no emojis, textContent-only.
  E. verify-live: BACKUP_MARKER emitted by the remote prelude; the
     check section maps ok/fail/stale/missing to OK/WARN (WARN-only,
     never FAIL).
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
INDEX = os.path.join(REPO, "manager", "static", "index.html")
VLIVE = os.path.join(REPO, "pending-deploy", "live-verify", "verify-live.sh")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def check(name, cond, detail=""):
    if cond:
        ok(name)
    else:
        bad(name, detail)


EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\u2B00-\u2BFF\uFE0F]")


def load_app():
    modname = "bv_app_under_test"
    if modname in sys.modules:
        del sys.modules[modname]
    spec = importlib.util.spec_from_file_location(modname, APP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    print("backup-visibility (python) tests")

    # ---------- A. read_backup_status units ----------
    mod = load_app()
    tmpd = tempfile.mkdtemp(prefix="bv_marker_")
    mpath = os.path.join(tmpd, "last-backup.json")
    mod.BACKUP_STATUS_FILE = mpath

    r = mod.read_backup_status()
    check("A1 missing marker -> known False", r == {
        "known": False, "ok": False, "ts": None, "age_h": None,
        "archive": None, "bytes": None, "error": ""}, str(r))

    open(mpath, "w").write("{not json")
    check("A2 corrupt marker -> known False",
          mod.read_backup_status()["known"] is False)

    open(mpath, "w").write("[1,2,3]")
    check("A3 non-dict JSON -> known False",
          mod.read_backup_status()["known"] is False)

    now = int(time.time())
    open(mpath, "w").write(json.dumps({
        "ts": "2026-10-15T03:17:05Z", "ts_epoch": now - 7200, "ok": True,
        "archive": "jerusalem-state-20261015-031705.tar.gz",
        "bytes": 47185920, "files": 9, "pruned": 2}))
    r = mod.read_backup_status()
    check("A4 ok:true shape", r["known"] is True and r["ok"] is True
          and r["archive"] == "jerusalem-state-20261015-031705.tar.gz"
          and r["bytes"] == 47185920 and r["error"] == ""
          and r["ts"] == "2026-10-15T03:17:05Z", str(r))
    check("A5 age_h ~2h", r["age_h"] is not None and 1.9 < r["age_h"] < 2.1,
          str(r.get("age_h")))

    open(mpath, "w").write(json.dumps({
        "ts": "2026-10-15T03:17:05Z", "ts_epoch": now - 3600, "ok": False,
        "error": "tar failed"}))
    r = mod.read_backup_status()
    check("A6 ok:false carries error", r["known"] is True and r["ok"] is False
          and r["error"] == "tar failed" and r["archive"] is None, str(r))

    open(mpath, "w").write(json.dumps({
        "ok": "true", "ts_epoch": "abc", "bytes": -5, "error": 123,
        "archive": 42, "ts": 99}))
    r = mod.read_backup_status()
    check("A7 garbage types coerced, never raises",
          r["known"] is True and r["ok"] is False and r["age_h"] is None
          and r["bytes"] is None and r["error"] == ""
          and r["archive"] is None and r["ts"] is None, str(r))

    open(mpath, "w").write(json.dumps({"ok": False, "ts_epoch": now,
                                       "error": "x" * 500}))
    r = mod.read_backup_status()
    check("A8 error truncated to 160", len(r["error"]) == 160, str(len(r["error"])))

    if os.geteuid() == 0:
        ok("A9 unreadable marker skipped (root reads everything; "
           "never-raises covered by A1-A3/A7)")
    else:
        os.chmod(mpath, 0)
        try:
            r = mod.read_backup_status()
            check("A9 unreadable marker -> known False (never raises)",
                  r["known"] is False, str(r))
        finally:
            os.chmod(mpath, 0o600)

    # ---------- B. route contract ----------
    src = open(APP, encoding="utf-8").read()
    do_get = src.index("def do_GET")
    do_post = src.index("def do_POST")
    body = src[do_get:do_post]
    check("B1 /api/backup-status inside do_GET",
          '"/api/backup-status"' in body)
    i_route = body.index('"/api/backup-status"')
    block = body[i_route:i_route + 400]
    i_auth = block.index("if not self._authed()")
    i_read = block.index("read_backup_status()")
    check("B2 auth gate before the read", i_auth < i_read)
    check("B3 401 shape", '{"error": "unauthorized"}' in block)

    # ---------- C. dashboard block, node-driven ----------
    isrc = open(INDEX, encoding="utf-8").read()
    m = re.search(r"// == backup-line begin ==\n(.*)\n// == backup-line end ==",
                  isrc, re.S)
    check("C0 marked block present", m is not None)
    block = m.group(1) if m else ""
    check("C0b block is textContent-only (no innerHTML)",
          "innerHTML" not in block)
    check("C0c no emojis in block", not EMOJI_RE.search(block))

    driver = block + r"""
;(async () => {
  const out = [];
  const els = {};
  const mkEl = () => ({ textContent: '', title: '', style: {} });
  global.$ = (id) => els[id] || (els[id] = mkEl());
  // stub api: first call ok, then configurable
  let calls = 0; let mode = 'ok';
  global.api = async (p) => { calls++;
    if (mode === 'throw') throw new Error('down');
    if (mode === 'fail') return {known:true, ok:false, ts:'t', age_h:5.0, archive:null, bytes:null, error:'tar failed'};
    if (mode === 'unknown') return {known:false, ok:false, ts:null, age_h:null, archive:null, bytes:null, error:''};
    return {known:true, ok:true, ts:'t', age_h:2.0, archive:'jerusalem-state-x.tar.gz', bytes:47185920, error:''};
  };
  const el = () => els['backupLine'];
  await updateBackupLine();
  out.push('T1:' + el().textContent + '|' + el().style.color + '|' + el().title);
  // cache: second call inside 5 min must not re-fetch
  await updateBackupLine();
  out.push('T2calls:' + calls);
  // fail mode, force refetch by aging the cache
  mode = 'fail'; backupCacheTs = 0; await updateBackupLine();
  out.push('T3:' + el().textContent + '|' + el().style.color + '|' + el().title);
  // unknown
  mode = 'unknown'; backupCacheTs = 0; await updateBackupLine();
  out.push('T4:' + el().textContent + '|' + el().style.color);
  // throwing api keeps old text
  const before = el().textContent; mode = 'throw'; backupCacheTs = 0; await updateBackupLine();
  out.push('T5kept:' + (el().textContent === before));
  // age strings
  out.push('T6a:' + backupAgeStr(0.2));
  out.push('T6b:' + backupAgeStr(2.0));
  out.push('T6c:' + backupAgeStr(50.0));
  out.push('T6d:' + backupAgeStr(null));
  // bytes null -> no MB part
  mode = 'ok'; backupCacheTs = 0;
  global.api = async () => ({known:true, ok:true, ts:'t', age_h:3.0, archive:null, bytes:null, error:''});
  await updateBackupLine();
  out.push('T7:' + el().textContent);
  console.log(out.join('\n'));
})().catch(e => { console.log('DRIVER-ERROR ' + e); process.exit(2); });
"""
    p = subprocess.run(["node", "-e", driver], capture_output=True, text=True,
                       timeout=30)
    lines = dict(l.split(":", 1) for l in p.stdout.strip().split("\n")
                 if ":" in l and not l.startswith("DRIVER"))
    check("C driver ran", p.returncode == 0 and "T1" in lines,
          p.stderr[:200] + p.stdout[:200])
    if "T1" in lines:
        t1 = lines["T1"].split("|")
        check("C1 ok renders green MB text",
              t1[0] == "Copia: hace 2h (45 MB)" and t1[1] == "var(--ok)"
              and t1[2] == "jerusalem-state-x.tar.gz", lines["T1"])
        check("C2 cache suppresses refetch", lines.get("T2calls") == "1",
              lines.get("T2calls"))
        t3 = lines["T3"].split("|")
        check("C3 fail renders red with reason",
              t3[0] == "Copia: falló — revisa" and t3[1] == "var(--bad)"
              and t3[2] == "tar failed", lines["T3"])
        t4 = lines["T4"].split("|")
        check("C4 unknown renders dim", t4[0] == "Copia: sin datos"
              and t4[1] == "var(--dim)", lines["T4"])
        check("C5 throwing api keeps old text", lines.get("T5kept") == "true")
        check("C6 age strings",
              lines.get("T6a") == " hace menos de 1h"
              and lines.get("T6b") == " hace 2h"
              and lines.get("T6c") == " hace 2d"
              and lines.get("T6d") == "", lines.get("T6a"))
        check("C7 null bytes omits MB",
              lines.get("T7") == "Copia: hace 3h", lines.get("T7"))

    # ---------- D. dashboard contract ----------
    check("D1 #backupLine span in fleetbar",
          re.search(r'id="resCov"></span><span class="fleetlabel" id="backupLine"></span>',
                    isrc) is not None)
    rm = re.search(r"async function refresh\(\) \{(.*?)\n\}", isrc, re.S)
    check("D2 refresh() calls updateBackupLine()",
          rm is not None and "updateBackupLine();" in rm.group(1))
    check("D3 Spanish labels present",
          "Copia: falló — revisa" in isrc and "Copia: sin datos" in isrc
          and "Copia:" in isrc)

    # ---------- E. verify-live probe ----------
    vsrc = open(VLIVE, encoding="utf-8").read()
    check("E1 remote prelude emits BACKUP_MARKER",
          'echo "BACKUP_MARKER=' in vsrc and "last-backup.json" in vsrc)
    check("E2 ok arm -> [OK]", re.search(
        r"ok:\*\).*ok \"last state backup", vsrc, re.S) is not None)
    check("E3 fail arm -> warn (never fail)",
          re.search(r"fail:\*\).*warn \"last state backup FAILED", vsrc, re.S)
          is not None)
    check("E4 stale arm -> warn",
          re.search(r"stale:\*\).*warn \"last state backup is stale", vsrc, re.S)
          is not None)
    check("E5 missing arm -> warn",
          re.search(r"\*\)\s+warn \"no last-backup.json marker", vsrc, re.S)
          is not None)
    check("E6 probe is WARN-only (no fail in its block)",
          "fail \"last state backup" not in vsrc
          and "fail \"no last-backup" not in vsrc)
    check("E7 verify-live.sh bash -n clean",
          subprocess.run(["bash", "-n", VLIVE]).returncode == 0)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
