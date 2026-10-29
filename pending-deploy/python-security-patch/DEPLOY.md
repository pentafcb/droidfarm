# DEPLOY: Python October 2026 security backports (python3.12)

Researched and staged 2026-10-06 ~19:50 UTC (worker session; no VPS SSH available).
Deploy blocked on VPS SSH access. Next session with SSH: run the commands below.

## Why
Python released coordinated security updates (~2026-10-01): 3.14.8, 3.13.16,
3.12.15, 3.11.17, 3.10.22 — 8 CVEs across tarfile, zipfile, SSL, urllib:
- CVE-2026-82049: tarfile extraction filter bypass (hard link to symlink escapes dest)
- CVE-2026-19672: tarfile "leave and return" path (`../evil/../dest/sub/file`)
- CVE-2026-15310: zipfile decompression bomb (unbounded per-read allocation)
- CVE-2026-15806: urllib HTTPPasswordMgr leaked HTTPS creds to HTTP URLs
- CVE-2026-19553: ssl.SSLContext.wrap_bio() arg validation (asyncio hostname too)
- CVE-2026-19445: ssl crash when SNI callback switches contexts
- CVE-2026-17084: stringprep/IDNA codepoint restriction
- plus bundled libexpat 2.8.5 and a float-formatting crash fix

Source: https://blog.python.org/2026/10/python-31022-31117/

## Our exposure (audited 2026-10-06 ~19:45 UTC in local sources)
- NO tarfile/zipfile extraction anywhere in manager/app.py or wing/agent.py.
- NO HTTPPasswordMgr / wrap_bio / SNI callback usage.
- urllib.request IS used: manager `wing_request` (node heartbeats, dashboard API
  calls, token auth) and wing agent heartbeats (urllib.request.urlopen).
  -> Patch by upgrading the OS python3.12 packages; the stdlib fixes ride along.
- Ubuntu 24.04 keeps `python3 --version` at 3.12.3; security fixes arrive as
  backported package revisions (3.12.3-1ubuntu0.N). DO NOT expect 3.12.15.

## Check (run on EACH box: main VPS 209.222.10.93 AND vultr 155.138.151.157)
```bash
python3 --version
dpkg -l | grep -E 'python3\.12|libpython3\.12' | awk '{print $2, $3}'
apt-get update -qq
apt list --upgradable 2>/dev/null | grep -E 'python3\.12|libpython3\.12' || echo "nothing upgradable"
```
If `apt list` shows any python3.12/libpython3.12 package upgradable, do the upgrade.

## Upgrade (root, both boxes) — scoped to python only, no full upgrade
```bash
DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade \
  python3.12 python3.12-minimal libpython3.12-minimal libpython3.12t64 \
  libpython3.12-stdlib python3.12-venv
```
(Validated: package names resolve on Ubuntu 24.04; `--dry-run` tested 2026-10-06.
aiortc/numpy/paramiko are pip packages — unaffected by this.)

## Restart services so they load the new libpython
```bash
# main VPS:
systemctl restart jerusalem
# vultr node (agent runs as wing-<hex>.service):
systemctl restart 'wing-*.service'
```

## Verify
1. `dpkg -l python3.12-minimal | tail -1` shows a NEWER revision than before
   (revision recorded in the check step above).
2. `systemctl is-active jerusalem` -> active.
3. `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/` -> 200.
4. Node agent health on vultr still reports OK via dashboard Nodes panel.
5. Manager logs clean: `journalctl -u jerusalem --since '5 min ago' | tail -20`.

## Notes / no action
- Behavior change (3.12): missing hostname + check_hostname now only warns
  (DeprecationWarning). We don't set check_hostname manually; aiortc unaffected.
- Waydroid image is Ubuntu 22.04 (python3.10) — 3.10 is now EOL (3.10.22 final,
  no more fixes ever). Containers only use python3 as a Dockerfile dependency
  (entrypoint + df-watchdog.sh inside containers are shell; the HOST watchdog
  uses the host's 3.12, covered above). Flagged as future work: rebuild the
  instance image on 24.04 when it's next rebuilt anyway. No rebuild tonight.
- If `apt list --upgradable` shows NOTHING for python3.12 on a box, Ubuntu may
  not have published the backport yet — re-check in 24h; the revision field
  (-1ubuntu0.N) is the thing to watch, not `python3 --version`.
