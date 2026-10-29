# boot-recovery

Host-boot fleet recovery for `df-watchdog.sh` (v14, section 0).

**Problem:** after a host reboot, all `wd-*` containers are stopped (no
`--restart` policy on `docker run`) and the binderfs pool is back to the
stock set. The watchdog's dead-container check used to fire for every
stopped instance in the same minute-cron pass — a 20-way Waydroid boot
storm — and `docker start` on containers whose bind-mounted
`/dev/binderfs/binderN` sources had vanished (docker auto-creates empty
dirs at missing bind sources → Waydroid boots with dead binder mounts →
stuck-boot wedges → circuit-budget DEGRADED storm).

**Fix:** section 0 runs before the per-instance loop on a fresh boot
(no `$ST/boot.done` sentinel + uptime < 900s): restores the binderfs
pool (mount + `binder-control` writes; empty docker stub dirs `rmdir`'d,
non-empty ones ALERTed and left alone), then stagger-starts the fleet
(3 `docker start`s per cycle, degraded skipped, 3 attempts/instance
before DEGRADED), then writes the sentinel. `check_one`'s down-branch
defers to it while active.

See `DEPLOY.md` for deploy/verify/rollback and
`test_boot_recovery.sh` (50 hermetic checks) for the behavior contract.
