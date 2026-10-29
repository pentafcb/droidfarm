# loop-device-guard

The fleet's kernel devices come in two pools: **binder** (3 per instance,
reserved at create) and **loop** (~2 per instance, consumed at Waydroid
boot). Binder got the full treatment — persistent modprobe.d name list,
create-time self-heal via binderfs, scripted verification. Loop had a
one-shot `mknod` in install.sh that evaporates on reboot (devtmpfs), no
manager self-heal, and no verification — a rebooted host silently capped
the fleet at ~4 booting instances.

## What this package adds

**Runtime self-heal (`manager/app.py`, staged via apply-all):**
- `count_loop_devices()` — `/dev/loopN` block-node count, never raises.
- `ensure_loop_devices(count=64, floor=60)` — if below floor: `modprobe
  loop max_loop=64` when the module isn't loaded (no-op when it is —
  max_loop can't change on a live module), then `mknod` for missing nodes.
  Returns False honestly when the live driver's `max_loop` parameter is
  below the floor (mknod nodes beyond it are unusable). Never raises; only
  internally-generated ints are interpolated into shell commands.
- `create_local_instance()` calls it after the image preflight. Unlike
  binder it **fails open**: loop is consumed at boot, not reserved at
  create, so a small fleet is fine on the stock 8. A loud `WARNING` goes to
  the journal and a `"warning"` string rides the 200 response (additive —
  the dashboard ignores unknown keys).
- `fleet_headroom()` (local branch) gains `loop_devices` + a warning when
  below floor — the dashboard's plan preview and confirm dialog already
  render `headroom.warning`, so no dashboard change was needed. Remote
  branch untouched (loop allocation on wing nodes is the agent's job).

**Fresh installs (`install.sh`, repo-side):** `install_loop_pool()` —
`options loop max_loop=64` in `/etc/modprobe.d/loop.conf`, `loop` appended
to `/etc/modules` (idempotent), immediate `modprobe` + `mknod` fallback,
count verification with WARN below 60. Path-overridable
(`LOOP_MODPROBE_D`/`LOOP_MODULES_FILE`/`LOOP_DEVDIR`) for hermetic tests.

**Verification (`binder-verify/verify-binder.sh`, repo-side):** sections
5–8 cover the loop chain — module loaded (WARN if invisible), 64 block
nodes (>=60 floor), live `max_loop` capacity check, and reboot
persistence via **either** `loop.max_loop=` on the kernel cmdline (the
old box's GRUB path) **or** modprobe.d + modules line. New `BV_PROC_CMDLINE`
override for tests. The morning `verify-binder.sh` run on the new VPS now
fails closed on a stock-8-loop host instead of passing and letting Create
fleet die at instance ~5.

## Design notes
- Binder fails closed at create (the triplet is bind-mounted into the
  container now); loop fails open with a warning (needed minutes later at
  boot, and small fleets don't need 60). The plan preview is the primary
  surface for the warning.
- The manager cannot fix a live module loaded with `max_loop=8` — that
  needs a module reload or reboot with persistent config. The code reports
  False and the verify script prints the exact remediation.
- Wing nodes: the agent does not self-heal loop devices (its installer
  only provisions binder). The vultr node runs a healthy fleet today; a
  wing-side `ensure_loop_devices` is a follow-up if a fresh wing node ever
  hits this.

## Tests
- `test_loop_device_guard.py` — 25/25 (hermetic, real app.py import):
  count/parse/garbage/raise, ensure healthy-noop/modprobe/mknod/
  max_loop-honesty/never-raises, create fail-open warning + reservation
  kept + binder-500 regression + gate ordering, headroom local/remote/
  low/healthy/raise, constants.
- `test_loop_device_guard.sh` — 30/30 (hermetic): verbatim-extracted
  `install_loop_pool()` (conf content, idempotent modules line, modprobe
  args, mknod only-missing, count + WARN, no real-path writes, bash -n)
  and the verify script's loop sections (boundaries 59/60/0, max_loop
  capacity FAIL, grub vs modprobe.d persistence, wrong-conf FAIL,
  modules-line FAIL, remediation text, read-only proof, --help/exit-2).

Preflight t66. Queue-audit 0 findings (9 checks).
