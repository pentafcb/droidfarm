#!/bin/bash
# Jerusalem 1.0 - Fresh install script
# Run as root: bash install.sh
# Clones from https://github.com/pentafcb/droidfarm if not already present
set -e

echo "=== Jerusalem 1.0 Installer ==="

# 1. System update
echo "[1/8] Updating system..."
apt-get update -qq
apt-get upgrade -y -qq 2>/dev/null || true

# 2. Install Docker
echo "[2/8] Installing Docker..."
if ! command -v docker &>/dev/null; then
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

# 3. Install Python + ffmpeg + dependencies
echo "[3/8] Installing Python, ffmpeg, and dependencies..."
apt-get install -y -qq python3 python3-pip git curl ffmpeg
pip3 install -q --break-system-packages aiortc numpy pillow 2>/dev/null || pip3 install -q aiortc numpy pillow

# 4. Kernel: persistent binder + loop device config for Waydroid fleet
# This is the critical fix: binder devices must survive reboot, or every
# fresh install hits "no free binder devices" on first instance create.
echo "[4/8] Configuring persistent kernel binder devices..."
# Load binder module on boot
if ! grep -q "^binder_linux" /etc/modules 2>/dev/null; then
    echo "binder_linux" >> /etc/modules
fi
# Configure 64 binder devices (supports ~21 instances at 3 devices each).
# NOTE: the binder 'devices' module parameter is a comma-separated NAME
# list, NOT a count — devices=64 would create a single device literally
# named "64" and zero /dev/binder* nodes. Build the name list explicitly.
# --- binder-names-begin ---
BINDER_NAMES="binder"
for i in $(seq 1 63); do BINDER_NAMES="$BINDER_NAMES,binder$i"; done
# --- binder-names-end ---
echo "options binder_linux devices=$BINDER_NAMES" > /etc/modprobe.d/binder.conf
# Reload the module so the modprobe.d options take effect now. modprobe
# applies modprobe.d options itself — never pass devices= on the command
# line. (If the module is busy and can't reload, the new options apply on
# next boot; the manager also self-heals the pool via binderfs at create.)
modprobe -r binder_linux 2>/dev/null || true
modprobe binder_linux 2>/dev/null || echo "WARNING: binder_linux module not available (may need reboot or different kernel)"
# Ensure loop devices exist (Waydroid needs ~2 per instance at boot; the
# 2026-10-08 fleet recovery needed ~40 for 20 instances). A one-shot mknod
# does NOT survive a reboot on devtmpfs — persist the pool size via
# modprobe.d and load the module on boot.
# --- loop-persist-begin ---
# install_loop_pool: persistent 64-loop-device provisioning. Idempotent.
# Path overrides (LOOP_MODPROBE_D / LOOP_MODULES_FILE) exist for hermetic
# testing; production calls use the real paths.
install_loop_pool() {
    local modprobe_d="${LOOP_MODPROBE_D:-/etc/modprobe.d}"
    local modules_file="${LOOP_MODULES_FILE:-/etc/modules}"
    local devdir="${LOOP_DEVDIR:-/dev}"
    if ! grep -q "^loop$" "$modules_file" 2>/dev/null; then
        echo "loop" >> "$modules_file"
    fi
    echo "options loop max_loop=64" > "$modprobe_d/loop.conf"
    # Immediate provisioning: modprobe applies modprobe.d options itself when
    # the module isn't loaded yet (a no-op when it already is — max_loop can't
    # change on a live module); mknod covers nodes missing under a capable
    # driver.
    modprobe loop max_loop=64 2>/dev/null || true
    local i
    for i in $(seq 0 63); do
        [ -e "$devdir/loop$i" ] || mknod "$devdir/loop$i" b 7 $i 2>/dev/null || true
    done
    # Verify (named block nodes only)
    local loop_count
    loop_count=$(ls -d "$devdir"/loop[0-9]* 2>/dev/null | wc -l)
    echo "Loop devices available: $loop_count"
    if [ "$loop_count" -lt 60 ]; then
        echo "WARNING: fewer than 60 loop nodes; the manager self-heals the pool"
        echo "at instance-create time (ensure_loop_devices), but a host reboot"
        echo "needs 'options loop max_loop=64' in $modprobe_d/loop.conf."
    fi
}
# --- loop-persist-end ---
install_loop_pool
# Verify (count the named nodes only, not the binderfs mount dir)
BINDER_COUNT=$(ls -d /dev/binder /dev/binder[0-9]* 2>/dev/null | wc -l)
echo "Binder devices available: $BINDER_COUNT"
if [ "$BINDER_COUNT" -lt 60 ]; then
    echo "WARNING: fewer than 60 binder nodes; the manager self-heals the pool"
    echo "via binderfs at instance-create time (ensure_binder_devices)."
fi

# 5. Get Jerusalem code
echo "[5/8] Setting up Jerusalem code..."
if [ -d /root/jerusalem/.git ]; then
    cd /root/jerusalem && git pull -q 2>/dev/null || true
    cd /root/jerusalem
else
    # If running from a repo clone, copy it; otherwise clone from GitHub
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$SCRIPT_DIR/manager/app.py" ]; then
        mkdir -p /root/jerusalem
        cp -r "$SCRIPT_DIR/"* /root/jerusalem/ 2>/dev/null || true
    else
        rm -rf /root/jerusalem
        git clone -q https://github.com/pentafcb/droidfarm /root/jerusalem
    fi
fi

# 6. Set up systemd service
echo "[6/8] Setting up systemd service..."
if [ -z "$JERUSALEM_PASSWORD" ]; then
    JERUSALEM_PASSWORD=$(openssl rand -hex 16)
    echo ""
    echo ">>> GENERATED DASHBOARD PASSWORD: $JERUSALEM_PASSWORD"
    echo ">>> Save this! You need it to log into the dashboard."
    echo ""
fi

cat > /etc/systemd/system/jerusalem.service << EOF
[Unit]
Description=Jerusalem 1.0 instance manager
After=docker.service
Requires=docker.service

[Service]
Type=simple
Environment=JERUSALEM_PASSWORD=$JERUSALEM_PASSWORD
Environment=JERUSALEM_PORT=8080
Environment=JERUSALEM_DATA=/root/jerusalem
WorkingDirectory=/root/jerusalem
ExecStart=/usr/bin/python3 /root/jerusalem/manager/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

mkdir -p /root/jerusalem
systemctl daemon-reload
systemctl enable jerusalem

# 7. Install hourly Docker log trimmer
echo "[7/9] Installing log trimmer..."
cat > /usr/local/bin/trim-docker-logs.sh << 'TRIM_EOF'
#!/bin/bash
for log in $(find /var/lib/docker/containers -name "*-json.log" 2>/dev/null); do
    size=$(stat -c%s "$log" 2>/dev/null || echo 0)
    if [ "$size" -gt 1073741824 ]; then
        tail -c 1073741824 "$log" > "$log.tmp" && mv "$log.tmp" "$log"
    fi
done
TRIM_EOF
chmod +x /usr/local/bin/trim-docker-logs.sh
# --- cron-ensure-begin ---
# ensure_cron: the watchdog minute-cron, the daily backup cron and the hourly
# log-trimmer below are all dead on a box without a running cron daemon
# (minimal images don't always ship one). Worse, the `crontab -l` pipeline
# fails under `set -e` when the crontab binary is missing, aborting the whole
# install at this step. Idempotent: a box with cron already active is a no-op.
ensure_cron() {
    if command -v systemctl >/dev/null 2>&1 && [ "$(systemctl is-active cron 2>/dev/null)" = "active" ]; then
        echo "cron already active"
        return 0
    fi
    # Install the package only when the crontab binary is missing entirely —
    # when it exists, the package is present and only the daemon needs
    # (re)starting. (Also keeps hermetic test envs with a fake crontab on
    # PATH from ever reaching a real apt-get.)
    if ! command -v crontab >/dev/null 2>&1; then
        apt-get install -y -qq cron 2>/dev/null || echo "WARNING: could not install the cron package"
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now cron 2>/dev/null || echo "WARNING: could not enable/start the cron daemon"
    else
        service cron start 2>/dev/null || echo "WARNING: could not start the cron daemon"
    fi
}
# --- cron-ensure-end ---
ensure_cron
(crontab -l 2>/dev/null | grep -v "trim-docker-logs"; echo "0 * * * * /usr/local/bin/trim-docker-logs.sh") | crontab -

# --- install-baseline-begin ---
# install_baseline: a fresh install.sh run gives the box a working dashboard
# but NO auto-recovery and NO backups — the minute-cron df-watchdog and the
# daily backup-state.sh cron are otherwise only installed by
# baseline-newbox.sh (the new VPS needed it as a separate manual run).
# Reuse that exact script (single source of truth) with the canonical
# sources from this repo. Warn-only by design: a baseline failure must
# never abort the install under set -e (the daily cron still lands its
# first archive at 03:17 once state exists).
install_baseline() {
    local repo_root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)}"
    local bl="$repo_root/pending-deploy/newbox-baseline/baseline-newbox.sh"
    local wd="$repo_root/waydroid-image/df-watchdog.sh"
    local bs="$repo_root/pending-deploy/backup-state/backup-state.sh"
    local vb="$repo_root/pending-deploy/backup-state/verify-backup.sh"
    local f
    for f in "$bl" "$wd" "$bs" "$vb"; do
        if [ ! -f "$f" ]; then
            echo "WARNING: host baseline source missing: $f (skipping watchdog + backup setup)"
            return 0
        fi
    done
    bash "$bl" "$wd" "$bs" "$vb" \
        || echo "WARNING: host baseline install failed (retry from an SSH session with baseline-newbox.sh)"
}
# --- install-baseline-end ---

# 8. Host baseline: watchdog minute-cron + daily state backup + log rotation
echo "[8/9] Installing host baseline (watchdog + backups)..."
install_baseline

# 9. Start service
echo "[9/9] Starting Jerusalem..."
systemctl restart jerusalem
sleep 3

echo ""
echo "=== Installation Complete ==="
echo "Dashboard: http://$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'):8080"
echo "Service status: $(systemctl is-active jerusalem)"
echo "Binder devices: $(ls /dev/binder* 2>/dev/null | wc -l)"
echo ""
echo "NEXT STEPS:"
echo "1. Build the Waydroid image (needs Roblox APKs in waydroid-image/):"
echo "   cd /root/jerusalem/waydroid-image && docker build -t waydroid-instance:latest ."
echo "2. Open the dashboard and create instances"
echo ""
echo "Check logs: journalctl -u jerusalem -f"
