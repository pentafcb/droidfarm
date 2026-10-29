#!/usr/bin/env python3
"""Tests for pending-deploy/queue-audit/audit-queue.sh (hermetic).

Builds fake pending-deploy trees in /tmp and runs the REAL audit-queue.sh
against them via PD_DIR/REPO env overrides, asserting pass/fail outcomes.
Also runs the auditor against the real repo tree (integration).
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "audit-queue.sh")

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def w(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def good_tree():
    """A fully-consistent fake queue tree. Returns (root, pd)."""
    root = tempfile.mkdtemp(prefix="qa-")
    pd = os.path.join(root, "pending-deploy")
    aa = os.path.join(pd, "apply-all")
    # repo-side sources referenced by fake SOURCES
    w(os.path.join(root, "a.py"), "# a\n")
    w(os.path.join(root, "b.sh"), "# b\n")
    w(os.path.join(aa, "deploy-all.sh"),
      'SOURCES="a.py b.sh"\n'
      'STAGE_NAMES="a.py b.sh"\n'
      'run "$REPO/pending-deploy/pkg-a/test_a.py"\n'
      'run "$REPO/pending-deploy/pkg-b/test_b.py"\n')
    w(os.path.join(aa, "vps-apply.sh"), 'KEYS="a.py b.sh"\n')
    w(os.path.join(aa, "APPLY.md"),
      "# APPLY\n## What's included\n| pkg-a | `a.py` |\n| pkg-b | `b.sh` |\n| live-verify | `verify-live.sh` |\n| post-deploy | `finish-deploy.sh` |\n"
      "## Deliberately NOT included\n| python-security-patch | apt |\n")
    for p in ("pkg-a", "pkg-b"):
        w(os.path.join(pd, p, "DEPLOY.md"), "# deploy\n")
    # fake post-deploy orchestrator: only runs staged scripts, and its STAGED
    # preflight list covers every staged file (minus itself)
    w(os.path.join(pd, "post-deploy", "DEPLOY.md"), "# deploy\n")
    w(os.path.join(pd, "post-deploy", "finish-deploy.sh"),
      '#!/bin/bash\n'
      'DF_DIR="${FD_DF_DIR:-/root/jerusalem}"\n'
      'STAGED="a.py b.sh"\n'
      'bash "$DF_DIR/b.sh"\n')
    w(os.path.join(pd, "pkg-a", "test_a.py"), "# t\n")
    w(os.path.join(pd, "pkg-b", "test_b.py"), "# t\n")
    w(os.path.join(pd, "python-security-patch", "DEPLOY.md"), "# apt\n")
    # fake live-verifier: probes every staged file via its remote for-list
    w(os.path.join(pd, "live-verify", "DEPLOY.md"), "# deploy\n")
    w(os.path.join(pd, "live-verify", "verify-live.sh"),
      "#!/bin/bash\n"
      "for f in a.py b.sh; do\n"
      '    echo "probe $f"\n'
      "done\n")
    # snapshot pair, in sync
    w(os.path.join(pd, "pkg-a", "snap.py"), "v1\n")
    w(os.path.join(root, "canon.py"), "v1\n")
    w(os.path.join(pd, "pkg-a", "SNAPSHOTS"),
      "pending-deploy/pkg-a/snap.py=canon.py\n")
    add_pins(root, pd, "2.740.931")
    return root, pd


PIN_WATCHDOG = 'PINNED_ROBLOX="${DF_PINNED_ROBLOX:-%s}"\n'
PIN_AGENT = 'PINNED_ROBLOX = os.environ.get("DF_PINNED_ROBLOX", "%s")\n'
PIN_FETCH = '# download the pinned Roblox %s split APKs\n'
PIN_EXTRACT = 'XAPK="${ROBLOX_XAPK:-$HOME/x/roblox-%s-x86_64.xapk}"\n'
PIN_SHA_BASE = 'PIN_BASE_SHA_DEFAULT="%s"\n'
PIN_SHA_X86 = 'PIN_X86_SHA_DEFAULT="%s"\n'
FAKE_SHA_BASE = "a" * 64
FAKE_SHA_X86 = "b" * 64


def add_pins(root, pd, ver):
    """Plant the four Roblox pin-definition files with one version."""
    w(os.path.join(root, "waydroid-image", "df-watchdog.sh"), PIN_WATCHDOG % ver)
    w(os.path.join(root, "wing", "agent.py"), PIN_AGENT % ver)
    w(os.path.join(pd, "waydroid-image-build", "fetch-roblox-apks.sh"),
      (PIN_FETCH % ver) + PIN_SHA_BASE % FAKE_SHA_BASE + PIN_SHA_X86 % FAKE_SHA_X86)
    w(os.path.join(pd, "waydroid-image-build", "extract-roblox-apks.sh"), PIN_EXTRACT % ver)
    w(os.path.join(pd, "waydroid-image-build", "vps-image-build.sh"),
      PIN_SHA_BASE % FAKE_SHA_BASE + PIN_SHA_X86 % FAKE_SHA_X86)
    # The pin files live in a package dir: the fake tree needs its DEPLOY.md
    # + APPLY.md row, or checks 1/5 trip on the scaffolding instead of the pins.
    w(os.path.join(pd, "waydroid-image-build", "DEPLOY.md"), "# deploy\n")
    md = os.path.join(pd, "apply-all", "APPLY.md")
    with open(md) as f:
        c = f.read()
    if "| waydroid-image-build |" not in c:
        w(md, c.replace("## Deliberately NOT included",
                        "| waydroid-image-build | `fetch-roblox-apks.sh` |\n"
                        "## Deliberately NOT included"))


def set_pin_ver(root, pd, which, ver):
    """Rewrite one pin file to a different version (partial-bump simulation)."""
    paths = {
        "watchdog": (os.path.join(root, "waydroid-image", "df-watchdog.sh"), PIN_WATCHDOG),
        "agent": (os.path.join(root, "wing", "agent.py"), PIN_AGENT),
        "extract": (os.path.join(pd, "waydroid-image-build", "extract-roblox-apks.sh"), PIN_EXTRACT),
    }
    if which == "fetch":
        # the fetch file also carries the SHA pins — keep them so the version
        # half (not the SHA half) is what trips
        w(os.path.join(pd, "waydroid-image-build", "fetch-roblox-apks.sh"),
          (PIN_FETCH % ver) + PIN_SHA_BASE % FAKE_SHA_BASE + PIN_SHA_X86 % FAKE_SHA_X86)
        return
    path, tmpl = paths[which]
    w(path, tmpl % ver)


def set_pin_sha(root, pd, which, role, sha):
    """Rewrite one file's PIN_<role>_SHA_DEFAULT (partial-bump simulation)."""
    if which == "fetch":
        path = os.path.join(pd, "waydroid-image-build", "fetch-roblox-apks.sh")
        prefix = PIN_FETCH % "2.740.931"
    else:
        path = os.path.join(pd, "waydroid-image-build", "vps-image-build.sh")
        prefix = ""
    base = sha if role == "BASE" else FAKE_SHA_BASE
    x86 = sha if role == "X86" else FAKE_SHA_X86
    w(path, prefix + PIN_SHA_BASE % base + PIN_SHA_X86 % x86)


def run_audit(root, pd):
    env = dict(os.environ, REPO=root, PD_DIR=pd)
    p = subprocess.run(["bash", AUDIT], env=env, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def mutate(root, pd, fn):
    fn(root, pd)
    return run_audit(root, pd)


# ---- 1. good tree passes ----
root, pd = good_tree()
rc, out = run_audit(root, pd)
check("good tree exits 0", rc == 0)
check("good tree reports 0 findings", "AUDIT: 0 finding(s)" in out)
check("good tree prints all 11 ok lines", out.count("ok: check") == 11)
shutil.rmtree(root, ignore_errors=True)

# ---- 2. missing DEPLOY.md ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: os.remove(os.path.join(p, "pkg-b", "DEPLOY.md")))
check("missing DEPLOY.md -> exit 1", rc == 1)
check("missing DEPLOY.md names package", "FAIL: package pkg-b: DEPLOY.md" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 3. empty DEPLOY.md ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: open(os.path.join(p, "pkg-a", "DEPLOY.md"), "w").close())
check("empty DEPLOY.md -> exit 1", rc == 1)
shutil.rmtree(root, ignore_errors=True)

# ---- 4. test file not invoked in preflight ----
root, pd = good_tree()
def add_orphan(r, p):
    w(os.path.join(p, "pkg-a", "test_orphan.py"), "# t\n")
rc, out = mutate(root, pd, add_orphan)
check("uninvoked test -> exit 1", rc == 1)
check("uninvoked test named", "test_orphan.py" in out and "NOT invoked" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 5. preflight invokes nonexistent test ----
root, pd = good_tree()
def add_ghost(r, p):
    da = os.path.join(p, "apply-all", "deploy-all.sh")
    with open(da, "a") as f:
        f.write('run "$REPO/pending-deploy/pkg-a/test_ghost.py"\n')
rc, out = mutate(root, pd, add_ghost)
check("ghost invocation -> exit 1", rc == 1)
check("ghost invocation named", "test_ghost.py" in out and "does not exist" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 6. duplicate invocation ----
root, pd = good_tree()
def add_dup(r, p):
    da = os.path.join(p, "apply-all", "deploy-all.sh")
    with open(da, "a") as f:
        f.write('run "$REPO/pending-deploy/pkg-a/test_a.py"\n')
rc, out = mutate(root, pd, add_dup)
check("duplicate invocation -> exit 1", rc == 1)
check("duplicate invocation says exactly-once", "exactly once" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 7. KEYS missing a stage file ----
root, pd = good_tree()
def drop_key(r, p):
    w(os.path.join(p, "apply-all", "vps-apply.sh"), 'KEYS="a.py"\n')
rc, out = mutate(root, pd, drop_key)
check("KEYS missing file -> exit 1", rc == 1)
check("KEYS mismatch message", "stage file set mismatch" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 8. KEYS has an extra file ----
root, pd = good_tree()
def add_key(r, p):
    w(os.path.join(p, "apply-all", "vps-apply.sh"), 'KEYS="a.py b.sh c.py"\n')
rc, out = mutate(root, pd, add_key)
check("KEYS extra file -> exit 1", rc == 1)
shutil.rmtree(root, ignore_errors=True)

# ---- 9. SOURCES/STAGE_NAMES count mismatch ----
root, pd = good_tree()
def break_pair(r, p):
    da = os.path.join(p, "apply-all", "deploy-all.sh")
    with open(da) as f:
        c = f.read()
    c = c.replace('STAGE_NAMES="a.py b.sh"', 'STAGE_NAMES="a.py"')
    w(da, c)
rc, out = mutate(root, pd, break_pair)
check("SOURCES/STAGE_NAMES count mismatch -> exit 1", rc == 1)
check("count mismatch message", "count mismatch" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 10. SOURCES entry missing from repo ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: os.remove(os.path.join(r, "b.sh")))
check("missing SOURCES file -> exit 1", rc == 1)
check("missing SOURCES named", "SOURCES entry missing from repo: b.sh" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 11. stale snapshot ----
root, pd = good_tree()
def stale_snap(r, p):
    w(os.path.join(r, "canon.py"), "v2 drifted\n")
rc, out = mutate(root, pd, stale_snap)
check("stale snapshot -> exit 1", rc == 1)
check("stale snapshot named", "STALE snapshot" in out and "snap.py" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 12. snapshot canonical missing ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: os.remove(os.path.join(r, "canon.py")))
check("missing canonical -> exit 1", rc == 1)
shutil.rmtree(root, ignore_errors=True)

# ---- 13. malformed SNAPSHOTS line ----
root, pd = good_tree()
def bad_snap_line(r, p):
    w(os.path.join(p, "pkg-a", "SNAPSHOTS"), "no-equals-here\n")
rc, out = mutate(root, pd, bad_snap_line)
check("malformed SNAPSHOTS line -> exit 1", rc == 1)
shutil.rmtree(root, ignore_errors=True)

# ---- 14. SNAPSHOTS comments/blanks tolerated ----
root, pd = good_tree()
def snap_comments(r, p):
    w(os.path.join(p, "pkg-a", "SNAPSHOTS"),
      "# comment\n\npending-deploy/pkg-a/snap.py=canon.py\n")
rc, out = mutate(root, pd, snap_comments)
check("SNAPSHOTS comments/blanks ok", rc == 0)
shutil.rmtree(root, ignore_errors=True)

# ---- 15. package missing from APPLY.md included section ----
root, pd = good_tree()
def drop_row(r, p):
    md = os.path.join(p, "apply-all", "APPLY.md")
    with open(md) as f:
        c = f.read()
    w(md, c.replace("| pkg-b | `b.sh` |\n", ""))
rc, out = mutate(root, pd, drop_row)
check("package dropped from included section -> exit 1", rc == 1)
check("included-section message", "What's included" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 16. package missing from APPLY.md entirely ----
root, pd = good_tree()
def drop_mention(r, p):
    md = os.path.join(p, "apply-all", "APPLY.md")
    with open(md) as f:
        c = f.read()
    w(md, c.replace("pkg-b", "pkg-B-renamed"))
rc, out = mutate(root, pd, drop_mention)
check("package unmentioned in APPLY.md -> exit 1", rc == 1)
shutil.rmtree(root, ignore_errors=True)

# ---- 17. orchestrator's own test exempt from preflight wiring ----
root, pd = good_tree()
w(os.path.join(pd, "apply-all", "test_apply_all.sh"), "# orchestrator self-test\n")
rc, out = run_audit(root, pd)
check("apply-all/test_apply_all.sh exempt (still 0)", rc == 0)
shutil.rmtree(root, ignore_errors=True)

# ---- 18. suite-less package (python-security-patch shape) passes ----
root, pd = good_tree()  # python-security-patch has DEPLOY.md, no test_*
rc, out = run_audit(root, pd)
check("suite-less package with DEPLOY.md passes", rc == 0)
shutil.rmtree(root, ignore_errors=True)

# ---- 19. check 6: legacy path in DEPLOY.md trips the audit ----
root, pd = good_tree()
def add_legacy_path(r, p):
    md = os.path.join(p, "pkg-a", "DEPLOY.md")
    with open(md, "a") as f:
        f.write("scp a.py root@h:/root/droidfarm/a.py\n")
rc, out = mutate(root, pd, add_legacy_path)
check("legacy /root/droidfarm path in DEPLOY.md -> exit 1", rc == 1)
check("legacy path finding names file", "legacy path in pkg-a/DEPLOY.md" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 20. check 6: legacy unit ref in a deploy script trips the audit ----
root, pd = good_tree()
def add_legacy_unit(r, p):
    sh = os.path.join(p, "apply-all", "deploy-all.sh")
    with open(sh, "a") as f:
        f.write("systemctl restart droidfarm\n")
rc, out = mutate(root, pd, add_legacy_unit)
check("legacy unit ref in deploy-all.sh -> exit 1", rc == 1)
check("legacy unit finding names file", "legacy unit ref in apply-all/deploy-all.sh" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 21. check 6 exemptions: test_* files, hyphenated names, legacy-guard lines ----
root, pd = good_tree()
def add_exempt(r, p):
    # test files may legitimately exercise the legacy guard
    w(os.path.join(p, "pkg-a", "test_legacy.py"), "# exercises LEGACY_ACTIVE with /root/droidfarm/x\n")
    sh = os.path.join(p, "apply-all", "deploy-all.sh")
    with open(sh, "a") as f:
        f.write('run "$REPO/pending-deploy/pkg-a/test_legacy.py"\n')
    # hyphenated names are not the manager unit
    with open(os.path.join(p, "pkg-b", "DEPLOY.md"), "a") as f:
        f.write("systemctl status droidfarm-wing\n")
    # lines marked legacy-guard are the guard itself
    with open(os.path.join(p, "apply-all", "vps-apply.sh"), "a") as f:
        f.write("systemctl is-active --quiet droidfarm # legacy-guard\n")
rc, out = mutate(root, pd, add_exempt)
check("check-6 exemptions still exit 0", rc == 0)
shutil.rmtree(root, ignore_errors=True)

# ---- 23. check 7: old product name in DEPLOY.md trips the audit ----
root, pd = good_tree()
def add_oldname(r, p):
    with open(os.path.join(p, "pkg-a", "DEPLOY.md"), "a") as f:
        f.write("verify: `journalctl -u jerusalem | grep DroidFarm` shows the banner\n")
rc, out = mutate(root, pd, add_oldname)
check("old product name in DEPLOY.md -> exit 1", rc == 1)
check("old product name finding names file", "old product name in pkg-a/DEPLOY.md" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 24. check 7 exemptions: legacy-marked, rename-history, test_* ----
root, pd = good_tree()
def add_name_exempt(r, p):
    with open(os.path.join(p, "pkg-a", "DEPLOY.md"), "a") as f:
        f.write("guard aborts if the legacy DroidFarm unit is active # legacy-guard\n")
        f.write("renamed from DroidFarm to Jerusalem 1.0 on 2026-10-08\n")
    w(os.path.join(p, "pkg-a", "test_oldname.py"), "# fixture greps the old DroidFarm banner\n")
    sh = os.path.join(p, "apply-all", "deploy-all.sh")
    with open(sh, "a") as f:
        f.write('run "$REPO/pending-deploy/pkg-a/test_oldname.py"\n')
rc, out = mutate(root, pd, add_name_exempt)
check("check-7 exemptions still exit 0", rc == 0)
shutil.rmtree(root, ignore_errors=True)

# ---- 26. check 8: staged file missing from the verify-live probe list ----
root, pd = good_tree()
def drop_probe(r, p):
    lv = os.path.join(p, "live-verify", "verify-live.sh")
    with open(lv) as f:
        content = f.read()
    content = content.replace("for f in a.py b.sh; do", "for f in a.py; do")
    with open(lv, "w") as f:
        f.write(content)
rc, out = mutate(root, pd, drop_probe)
check("unprobed staged file -> exit 1", rc == 1)
check("unprobed staged file named", "b.sh" in out and "verify-live" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 27. check 8: df-watchdog.sh covered by the SHA_watchdog probe, not the for-list ----
def add_watchdog(r, p, with_probe):
    w(os.path.join(r, "w.sh"), "# w\n")
    da = os.path.join(p, "apply-all", "deploy-all.sh")
    with open(da) as f:
        content = f.read()
    content = content.replace('SOURCES="a.py b.sh"', 'SOURCES="a.py b.sh w.sh"')
    content = content.replace('STAGE_NAMES="a.py b.sh"', 'STAGE_NAMES="a.py b.sh df-watchdog.sh"')
    with open(da, "w") as f:
        f.write(content)
    va = os.path.join(p, "apply-all", "vps-apply.sh")
    with open(va) as f:
        vcontent = f.read()
    vcontent = vcontent.replace('KEYS="a.py b.sh"', 'KEYS="a.py b.sh df-watchdog.sh"')
    with open(va, "w") as f:
        f.write(vcontent)
    # check 9: the new staged file must also be in the orchestrator's STAGED list
    fd = os.path.join(p, "post-deploy", "finish-deploy.sh")
    with open(fd) as f:
        fcontent = f.read()
    fcontent = fcontent.replace('STAGED="a.py b.sh"', 'STAGED="a.py b.sh df-watchdog.sh"')
    with open(fd, "w") as f:
        f.write(fcontent)
    if with_probe:
        lv = os.path.join(p, "live-verify", "verify-live.sh")
        with open(lv, "a") as f:
            f.write('echo "SHA_watchdog=abc"\n')
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: add_watchdog(r, p, True))
check("df-watchdog.sh with SHA_watchdog probe -> exit 0", rc == 0)
shutil.rmtree(root, ignore_errors=True)

# ---- 28. check 8: df-watchdog.sh without the SHA_watchdog probe trips the audit ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: add_watchdog(r, p, False))
check("df-watchdog.sh without probe -> exit 1", rc == 1)
check("df-watchdog.sh finding names file", "df-watchdog.sh" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 29. check 9: finish-deploy.sh running an unstaged script trips the audit ----
root, pd = good_tree()
def add_unstaged_ref(r, p):
    fd = os.path.join(p, "post-deploy", "finish-deploy.sh")
    with open(fd, "a") as f:
        f.write('bash "$DF_DIR/c.sh"\n')
rc, out = mutate(root, pd, add_unstaged_ref)
check("unstaged $DF_DIR ref -> exit 1", rc == 1)
check("unstaged ref names the file", "c.sh" in out and "check 9" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 30. check 9: staged file missing from the STAGED preflight list trips the audit ----
root, pd = good_tree()
def drop_staged(r, p):
    fd = os.path.join(p, "post-deploy", "finish-deploy.sh")
    with open(fd) as f:
        content = f.read()
    content = content.replace('STAGED="a.py b.sh"', 'STAGED="a.py"')
    with open(fd, "w") as f:
        f.write(content)
rc, out = mutate(root, pd, drop_staged)
check("staged file missing from STAGED -> exit 1", rc == 1)
check("missing STAGED entry names the file", "b.sh" in out and "check 9" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 31. check 9: missing finish-deploy.sh fails closed ----
root, pd = good_tree()
def rm_fd(r, p):
    os.remove(os.path.join(p, "post-deploy", "finish-deploy.sh"))
rc, out = mutate(root, pd, rm_fd)
check("missing finish-deploy.sh -> exit 1", rc == 1)
check("missing finish-deploy.sh named", "finish-deploy.sh" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 32. check 5: name-prefix stub trips the audit (whole-word match) ----
# Regression for the 2026-10-18 retired stub: an untracked package dir whose
# name is a strict prefix of a documented package (android-optimize vs
# android-optimizer) passed check 5 on a substring match. With whole-word
# matching it must fail closed.
root, pd = good_tree()
def add_documented(r, p, name):
    w(os.path.join(p, name, "DEPLOY.md"), "# deploy\n")
    md = os.path.join(p, "apply-all", "APPLY.md")
    with open(md) as f:
        c = f.read()
    w(md, c.replace("## Deliberately NOT included",
                    f"| {name} | `x.sh` |\n## Deliberately NOT included"))
def add_prefix_stub(r, p):
    add_documented(r, p, "android-optimizer")
    w(os.path.join(p, "android-optimize", "DEPLOY.md"), "# deploy\n")  # the stub
rc, out = mutate(root, pd, add_prefix_stub)
check("name-prefix stub -> exit 1", rc == 1)
check("name-prefix stub named", "package android-optimize" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 33. check 5: both names documented as words passes ----
root, pd = good_tree()
def add_both_words(r, p):
    add_documented(r, p, "android-optimizer")
    add_documented(r, p, "android-optimize")
rc, out = mutate(root, pd, add_both_words)
check("both names documented as words -> exit 0", rc == 0)
shutil.rmtree(root, ignore_errors=True)

# ---- 34. check 10: pin agreement passes and names the pin ----
root, pd = good_tree()
rc, out = run_audit(root, pd)
check("pin agreement: exit 0", rc == 0)
check("pin agreement: ok line names the pin", "ok: check 10" in out and "2.740.931" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 35. check 10: watchdog pin bumped alone trips the audit ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: set_pin_ver(r, p, "watchdog", "2.743.1136"))
check("watchdog-only bump -> exit 1", rc == 1)
check("watchdog-only bump cites check 10", "check 10" in out and "mismatch" in out)
check("watchdog-only bump shows both versions", "2.743.1136" in out and "2.740.931" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 36. check 10: agent pin bumped alone trips the audit ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: set_pin_ver(r, p, "agent", "2.743.1136"))
check("agent-only bump -> exit 1", rc == 1)
check("agent-only bump cites check 10", "check 10" in out and "agent.py" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 37. check 10: fetch-roblox-apks.sh documented pin bumped alone trips the audit ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: set_pin_ver(r, p, "fetch", "2.743.1136"))
check("fetch-only bump -> exit 1", rc == 1)
check("fetch-only bump cites check 10", "check 10" in out and "fetch-roblox-apks.sh" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 38. check 10: extract-roblox-apks.sh XAPK bumped alone trips the audit ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: set_pin_ver(r, p, "extract", "2.743.1136"))
check("extract-only bump -> exit 1", rc == 1)
check("extract-only bump cites check 10", "check 10" in out and "extract-roblox-apks.sh" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 39. check 10: missing pin file fails closed ----
root, pd = good_tree()
def drop_pin_file(r, p):
    os.remove(os.path.join(r, "wing", "agent.py"))
rc, out = mutate(root, pd, drop_pin_file)
check("missing pin file -> exit 1", rc == 1)
check("missing pin file named", "agent.py" in out and "check 10" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 40. check 10: unparsable pin definition fails closed ----
root, pd = good_tree()
def unparseable_pin(r, p):
    w(os.path.join(r, "waydroid-image", "df-watchdog.sh"),
      'PINNED_ROBLOX="$DF_PINNED_ROBLOX"\n')  # refactored away the default
rc, out = mutate(root, pd, unparseable_pin)
check("unparsable pin -> exit 1", rc == 1)
check("unparsable pin cites check 10", "check 10" in out and "cannot extract" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 41. check 10: conflicting pins within one file fail closed ----
root, pd = good_tree()
def conflicting_pins(r, p):
    w(os.path.join(p, "waydroid-image-build", "fetch-roblox-apks.sh"),
      '# download the pinned Roblox 2.740.931 split APKs\n'
      '# the pinned 2.743.1136 split would fail the sha check\n')
rc, out = mutate(root, pd, conflicting_pins)
check("conflicting pins -> exit 1", rc == 1)
check("conflicting pins cites check 10", "check 10" in out and "conflicting" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 42. check 10: SHA pins agree on the good tree ----
root, pd = good_tree()
rc, out = run_audit(root, pd)
check("sha agreement: exit 0", rc == 0)
check("sha agreement: ok line printed", "ok: check 10: Roblox SHA pins agree" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 43. check 10: fetch BASE sha bumped alone trips the audit ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: set_pin_sha(r, p, "fetch", "BASE", "c" * 64))
check("fetch-only sha bump -> exit 1", rc == 1)
check("fetch-only sha bump cites check 10", "check 10" in out and "mismatch" in out)
check("fetch-only sha bump names the pin", "PIN_BASE_SHA_DEFAULT" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 44. check 10: vps X86 sha bumped alone trips the audit ----
root, pd = good_tree()
rc, out = mutate(root, pd, lambda r, p: set_pin_sha(r, p, "vps", "X86", "d" * 64))
check("vps-only sha bump -> exit 1", rc == 1)
check("vps-only sha bump cites check 10", "check 10" in out and "mismatch" in out)
check("vps-only sha bump names the pin", "PIN_X86_SHA_DEFAULT" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 45. check 10: missing vps-image-build.sh fails closed ----
root, pd = good_tree()
def drop_vps_build(r, p):
    os.remove(os.path.join(p, "waydroid-image-build", "vps-image-build.sh"))
rc, out = mutate(root, pd, drop_vps_build)
check("missing vps build script -> exit 1", rc == 1)
check("missing vps build script named", "vps-image-build.sh" in out and "check 10" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 46. check 10: duplicated SHA definition in one file fails closed ----
root, pd = good_tree()
def duplicated_sha(r, p):
    w(os.path.join(p, "waydroid-image-build", "fetch-roblox-apks.sh"),
      (PIN_FETCH % "2.740.931") + PIN_SHA_BASE % FAKE_SHA_BASE
      + PIN_SHA_BASE % FAKE_SHA_X86 + PIN_SHA_X86 % FAKE_SHA_X86)
rc, out = mutate(root, pd, duplicated_sha)
check("duplicated sha -> exit 1", rc == 1)
check("duplicated sha cites check 10", "check 10" in out and "duplicated" in out)
shutil.rmtree(root, ignore_errors=True)

# ---- 25. integration: real repo tree audits clean ----
env = dict(os.environ)
env.pop("REPO", None)
env.pop("PD_DIR", None)
p = subprocess.run(["bash", AUDIT], env=env, capture_output=True, text=True)
check("real repo tree: exit 0", p.returncode == 0)
check("real repo tree: 0 findings", "AUDIT: 0 finding(s)" in (p.stdout + p.stderr))
if p.returncode != 0:
    print("  auditor output on real tree:\n" + p.stdout + p.stderr)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
