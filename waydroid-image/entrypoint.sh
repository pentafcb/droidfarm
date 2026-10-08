#!/bin/bash
# Entrypoint for a Waydroid-in-Docker instance.
# Expects: /dev/binder, /dev/vndbinder, /dev/hwbinder (per-instance binderfs devices).
# Env: INSTANCE_ID (numeric, selects network bridge/subnet).
# On first boot: patches binder names, sets up network, installs Roblox automatically.
set -e

echo "[entrypoint] binder devices:"
ls -la /dev/binder /dev/vndbinder /dev/hwbinder 2>&1

# D-Bus (clean stale sockets from unclean shutdowns)
rm -f /run/dbus/system_bus_socket /run/dbus/pid 2>/dev/null || true
pkill -f "dbus-daemon --system" 2>/dev/null || true
sleep 1
mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true
export DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address 2>/dev/null)
echo "[entrypoint] session bus: ${DBUS_SESSION_BUS_ADDRESS:-FAILED}"

# First-run init (binder patches only run here)
if [ ! -f /var/lib/waydroid/waydroid.cfg ]; then
    echo "[entrypoint] running waydroid init (VANILLA)..."
    waydroid init -f
    echo "[entrypoint] patching binder device names in waydroid.cfg..."
    sed -i 's/^binder *=.*/binder = binder/' /var/lib/waydroid/waydroid.cfg
    sed -i 's/^vndbinder *=.*/vndbinder = vndbinder/' /var/lib/waydroid/waydroid.cfg
    sed -i 's/^hwbinder *=.*/hwbinder = hwbinder/' /var/lib/waydroid/waydroid.cfg
    sed -i 's/anbox-binder/binder/g; s/anbox-vndbinder/vndbinder/g; s/anbox-hwbinder/hwbinder/g' /var/lib/waydroid/waydroid.cfg
    echo "[entrypoint] patching lxc config_nodes..."
    if [ -f /var/lib/waydroid/lxc/waydroid/config_nodes ]; then
        sed -i 's|/dev/anbox-vndbinder|/dev/vndbinder|g; s|/dev/anbox-hwbinder|/dev/hwbinder|g; s|/dev/anbox-binder|/dev/binder|g' /var/lib/waydroid/lxc/waydroid/config_nodes
    fi
    grep -i "^binder\|^vndbinder\|^hwbinder" /var/lib/waydroid/waydroid.cfg || true
fi

# Per-instance network bridge (avoids waydroid0 conflicts between instances).
# Runs on EVERY start because bridges don't survive container restarts.
IID="${INSTANCE_ID:-0}"
BRIDGE="wdb${IID}"
SUBNET="192.168.$(( (240 + IID) % 250 + 1 ))"
echo "[entrypoint] setting up network bridge ${BRIDGE} (${SUBNET}.0/24)..."
if [ -f /var/lib/waydroid/lxc/waydroid/config ]; then
    sed -i "s/^lxc.net.0.link *=.*/lxc.net.0.link = ${BRIDGE}/" /var/lib/waydroid/lxc/waydroid/config
fi
if ! ip link show "$BRIDGE" >/dev/null 2>&1; then
    ip link add "$BRIDGE" type bridge
    ip addr add "${SUBNET}.1/24" dev "$BRIDGE"
    ip link set "$BRIDGE" up
    iptables -t nat -C POSTROUTING -s "${SUBNET}.0/24" -j MASQUERADE 2>/dev/null || \
        iptables -t nat -A POSTROUTING -s "${SUBNET}.0/24" -j MASQUERADE
    iptables -C FORWARD -i "$BRIDGE" -j ACCEPT 2>/dev/null || \
        iptables -A FORWARD -i "$BRIDGE" -j ACCEPT
    iptables -C FORWARD -o "$BRIDGE" -j ACCEPT 2>/dev/null || \
        iptables -A FORWARD -o "$BRIDGE" -j ACCEPT
fi
if ! pgrep -f "dnsmasq.*${BRIDGE}" >/dev/null 2>&1; then
    dnsmasq --interface="$BRIDGE" --bind-interfaces \
        --dhcp-range="${SUBNET}.2,${SUBNET}.254,12h" \
        --dhcp-option=6,8.8.8.8,8.8.4.4 \
        --pid-file="/tmp/dnsmasq-${BRIDGE}.pid" 2>&1 || \
        echo "[entrypoint] WARNING: dnsmasq failed for ${BRIDGE}"
fi
echo "[entrypoint] network ready: ${BRIDGE} ${SUBNET}.1"

# Headless Wayland
export XDG_RUNTIME_DIR=/tmp/xdg-runtime
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
export WAYLAND_DISPLAY=wayland-0
echo "[entrypoint] starting headless weston..."
weston --backend=headless-backend.so --socket=wayland-0 --idle-time=0 --log=/tmp/weston.log &
sleep 4

# PulseAudio
echo "[entrypoint] starting pulseaudio..."
pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
sleep 2

echo "[entrypoint] starting waydroid container service..."
waydroid container start &
sleep 4

echo "[entrypoint] starting waydroid session..."
waydroid session start &
sleep 5

echo "[entrypoint] waiting for Android boot..."
BOOTED=0
for i in $(seq 1 60); do
    if waydroid shell getprop sys.boot_completed 2>/dev/null | grep -q 1; then
        echo "[entrypoint] Android booted."
        BOOTED=1
        break
    fi
    sleep 5
done

if [ "$BOOTED" = "1" ]; then
    mkdir -p /root/.local/share/waydroid/data/media/0/droidfarm
    touch /root/.local/share/waydroid/data/media/0/droidfarm/booted
    if waydroid shell pm list packages 2>/dev/null | grep -q "com.roblox.client"; then
        echo "[entrypoint] Roblox already installed."
        touch /root/.local/share/waydroid/data/media/0/droidfarm/roblox_done
    else
        echo "[entrypoint] installing Roblox 2.740.931 (split APKs)..."
        if [ -f /opt/roblox-base.apk ] && [ -f /opt/roblox-x86_64.apk ]; then
            mkdir -p /root/.local/share/waydroid/data/media/0/apks
            cp /opt/roblox-base.apk /root/.local/share/waydroid/data/media/0/apks/
            cp /opt/roblox-x86_64.apk /root/.local/share/waydroid/data/media/0/apks/
            if waydroid shell pm install /data/media/0/apks/roblox-base.apk /data/media/0/apks/roblox-x86_64.apk 2>&1 | grep -q "Success"; then
                echo "[entrypoint] Roblox installed successfully."
                touch /root/.local/share/waydroid/data/media/0/droidfarm/roblox_done
            else
                echo "[entrypoint] WARNING: Roblox install failed."
            fi
        else
            echo "[entrypoint] WARNING: Roblox APKs not found, skipping."
        fi
    fi
    waydroid shell input keyevent 4 2>/dev/null || true

    # Start persistent screencap loop for fast dashboard streaming.
    # Writes to /data/media/0/droidfarm/live.png which the host reads
    # directly from the docker volume (no per-frame exec overhead).
    # Uses atomic rename to avoid the dashboard reading a partial file
    # (which causes black flicker).
    mkdir -p /root/.local/share/waydroid/data/media/0/droidfarm
    printf '#!/bin/sh\nwhile true; do\n  timeout 5 screencap -p /data/media/0/droidfarm/live.tmp.png 2>/dev/null\n  mv /data/media/0/droidfarm/live.tmp.png /data/media/0/droidfarm/live.png 2>/dev/null\n  sleep 0.12\ndone\n' > /root/.local/share/waydroid/data/media/0/droidfarm/screencap-loop.sh
    chmod +x /root/.local/share/waydroid/data/media/0/droidfarm/screencap-loop.sh
    # Run in background via single waydroid shell invocation (detached)
    nohup waydroid shell sh /data/media/0/droidfarm/screencap-loop.sh >/dev/null 2>&1 &
    echo "[entrypoint] screencap loop started."
    # Input daemon: persistent shell reading commands from FIFO for instant input
    # (avoids 27s waydroid shell startup per tap). API writes to the FIFO via volume.
    printf '#!/bin/sh\nFIFO=/data/media/0/droidfarm/input.fifo\nrm -f $FIFO\nmkfifo $FIFO\nwhile true; do\n  if read cmd < $FIFO; then\n    eval "$cmd" 2>/dev/null\n  fi\ndone\n' > /root/.local/share/waydroid/data/media/0/droidfarm/input-daemon.sh
    chmod +x /root/.local/share/waydroid/data/media/0/droidfarm/input-daemon.sh
    nohup waydroid shell sh /data/media/0/droidfarm/input-daemon.sh >/dev/null 2>&1 &
    echo "[entrypoint] input daemon started."

    # Roblox keepalive: force Roblox to always stay in foreground.
    # Uses input taps (monkey/am do not work in this Waydroid image).
    # v2 (crash-loop circuit breaker: launch-attempt budget, backoff, and a
    # keepalive.degraded marker the host watchdog surfaces on the dashboard)
    # lives in waydroid-image/roblox-keepalive.sh and is baked into the image
    # at /opt/roblox-keepalive.sh. The canonical copy is written into the
    # shared data dir at boot so the watchdog and live-container pushes all
    # use the same file.
    if [ -f /opt/roblox-keepalive.sh ]; then
        cp -f /opt/roblox-keepalive.sh /root/.local/share/waydroid/data/media/0/droidfarm/roblox-keepalive.sh
        echo "[entrypoint] Roblox keepalive v2 installed from /opt/roblox-keepalive.sh."
    else
        echo "[entrypoint] WARN /opt/roblox-keepalive.sh missing - legacy inline keepalive (no crash-loop breaker)"
        printf '#!/bin/sh\nwhile true; do\n  FOCUS=$(dumpsys window 2>/dev/null | grep "mCurrentFocus" | head -1)\n  case "$FOCUS" in\n    *com.roblox.client*) ;;\n    *) input keyevent 3; sleep 1; input swipe 512 400 512 100; sleep 2; input tap 129 424 ;;\n  esac\n  sleep 10\ndone\n' > /root/.local/share/waydroid/data/media/0/droidfarm/roblox-keepalive.sh
    fi
    chmod +x /root/.local/share/waydroid/data/media/0/droidfarm/roblox-keepalive.sh
    nohup waydroid shell sh /data/media/0/droidfarm/roblox-keepalive.sh >/dev/null 2>&1 &
    echo "[entrypoint] Roblox keepalive started."
    # Android OS optimization: debloat, stay-awake, no-doze, cache drop.
    # NOTE: `settings put` is broken in this Waydroid build (AppOps NPE from the
    # root shell), so this uses only commands verified to work: pm, svc, dumpsys.
    # Written to the shared dir so it runs inside Android via waydroid shell.
    cat > /root/.local/share/waydroid/data/media/0/droidfarm/android-optimize.sh <<'OPT_EOF'
#!/bin/sh
S=/data/media/0/droidfarm
{
echo "[optimize] run at $(date)"
# Debloat: disable unused system packages (reversible, user 0 only)
for p in com.android.printspooler com.android.wallpaper.livepicker com.android.stk com.android.cellbroadcastreceiver com.android.bookmarkprovider; do
  pm disable-user --user 0 "$p" >/dev/null 2>&1
done
# Never sleep while streaming (black-screen killer); persists in settings
svc power stayon true >/dev/null 2>&1
# Disable Doze (runtime state; re-applied every boot by this script)
dumpsys deviceidle disable >/dev/null 2>&1
# Trim app caches to free storage
pm trim-caches 999G >/dev/null 2>&1
echo "[optimize] done"
} >> $S/optimize.log 2>&1
OPT_EOF
    chmod +x /root/.local/share/waydroid/data/media/0/droidfarm/android-optimize.sh
    waydroid shell sh /data/media/0/droidfarm/android-optimize.sh >/dev/null 2>&1 &
    echo "[entrypoint] Android optimizer started."
    # Launch Roblox immediately (tap sequence; monkey does not work here)
    waydroid shell "input keyevent 3; sleep 1; input swipe 512 400 512 100; sleep 2; input tap 129 424" >/dev/null 2>&1 &
    echo "[entrypoint] Roblox launched."
else
    echo "[entrypoint] WARNING: Android did not boot in time."
fi

echo "[entrypoint] ready. Holding container alive."
exec tail -f /dev/null



