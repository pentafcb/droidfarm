# wing-binderfs-selfheal

Agent 1.0.16: the wing agent now self-heals the binderfs device pool on
instance create, matching the manager's `ensure_binder_devices()` from the
2026-10-07 binderfs-selfheal package.

Why: a host reboot drops `/dev/binderfs/` back to the stock device set.
Before 1.0.16, the first create on a rebooted wing node died with
"binder devices not found" and every later create died the same way until a
human recreated the pool by hand (the BINDER_CTL_ADD ioctl dance).

Now `create_instance_linux` finds no complete triplet -> calls
`sw_ensure_binder_devices()` (modprobe binder_linux if the module is
missing, mount binderfs if binder-control is missing, then create
binder, binder1..binder63 through binder-control) -> re-scans once ->
proceeds with the create, or fails honestly when the pool is unhealable.

Safety: names are generated internally (no user input, `binder\d*` only);
best-effort everywhere, never raises; Windows nodes return False (the pool
concept is Linux-only); no self-heal shell writes when a triplet already
exists, so the hot path is untouched.
