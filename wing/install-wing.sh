#!/bin/bash
MAIN_URL="__MAIN_URL__"
JOIN_TOKEN=__JOIN_TOKEN__
INSTALL_ID=$(od -An -tx1 -N4 /dev/urandom 2>/dev/null | tr -d ' \n')
[ ${#INSTALL_ID} -ge 8 ] || INSTALL_ID="i$(date +%s%N | tail -c8)"
WNAME=$(od -An -tx1 -N4 /dev/urandom 2>/dev/null | tr -d ' \n')
[ ${#WNAME} -ge 8 ] || WNAME=$(cat /proc/sys/kernel/random/uuid 2>/dev/null | tr -d '-' | head -c8)
[ ${#WNAME} -ge 8 ] || WNAME="a$(date +%s%N | tail -c8)"
export MAIN_URL JOIN_TOKEN WNAME INSTALL_ID
WDIR="/root/$WNAME"
mkdir -p "$WDIR" 2>/dev/null

[ "$(id -u)" != "0" ] && exit 1

bash -c '
WNAME="$WNAME"
WDIR="/root/$WNAME"
LOG="$WDIR/install.log"
MAIN_URL="$MAIN_URL"
JOIN_TOKEN=__JOIN_TOKEN__
INSTALL_ID="$INSTALL_ID"
HOSTNAME="$(hostname 2>/dev/null || echo unknown)"
exec 3>&1 4>&2
exec >>"$LOG" 2>&1
report() {
  echo "[$(date +%H:%M:%S)] [$1] $2" >&3
  echo "[$(date +%H:%M:%S)] [$1] $2"
  curl -sSL -m 10 -X POST -H "Content-Type: application/json" \
    -d "{\"join_token\":\"$JOIN_TOKEN\",\"install_id\":\"$INSTALL_ID\",\"hostname\":\"$HOSTNAME\",\"os\":\"Linux\",\"stage\":\"$1\",\"message\":\"$2\"}" \
    "$MAIN_URL/api/providers/install-progress" >/dev/null 2>&1 || true
}
fail() { echo "FATAL: $1" >&3; echo "FATAL: $1"; report "failed" "$1"; exit 1; }
echo "=== Wing installer started (log: $LOG) ===" >&3
report "started" "installer launched"
report "cleanup" "removing previous install"

for svc in $(timeout 10 systemctl list-units --all --type=service --no-legend 2>/dev/null | awk "{print \$1}" | grep -E "^[a-f0-9]{8}\\.service$" 2>/dev/null); do
  timeout 8 systemctl stop "$svc" 2>/dev/null || true
  timeout 8 systemctl disable "$svc" 2>/dev/null || true
  rm -f "/etc/systemd/system/$svc" 2>/dev/null || true
done
for pid in $(pgrep -f "agent\.py" 2>/dev/null); do
  if [ "$pid" != "$$" ] && [ "$pid" != "$PPID" ]; then
    kill "$pid" 2>/dev/null || true
  fi
done
for d in /root/*/; do
  if [ -f "$d/config.json" ] && [ -f "$d/agent.py" ]; then
    rm -rf "$d" 2>/dev/null || true
  fi
done
timeout 10 systemctl daemon-reload 2>/dev/null || true
mkdir -p "$WDIR" 2>/dev/null || true
report "cleanup" "previous install removed (if any)"

report "deps" "updating system packages"
if command -v apt-get >/dev/null 2>&1; then
  report "deps" "running apt update"
  echo "--- apt update ---" >&3
  apt-get update 2>&1 | tee /dev/fd/3 | tail -5
  report "deps" "running apt upgrade"
  echo "--- apt upgrade ---" >&3
  DEBIAN_FRONTEND=noninteractive apt-get upgrade -y 2>&1 | tee /dev/fd/3 | tail -5
elif command -v dnf >/dev/null 2>&1; then
  report "deps" "running dnf upgrade"
  dnf upgrade -y 2>&1 | tee /dev/fd/3 | tail -5 || true
elif command -v yum >/dev/null 2>&1; then
  report "deps" "running yum update"
  yum update -y 2>&1 | tee /dev/fd/3 | tail -5 || true
fi

report "deps" "installing base dependencies"
echo "--- installing dependencies ---" >&3
if command -v apt-get >/dev/null 2>&1; then
  apt-get install -y python3 python3-pip curl ca-certificates gnupg lsb-release iptables iproute2 2>&1 | tee /dev/fd/3 | tail -5 || fail "could not install dependencies via apt"
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip curl ca-certificates gnupg iptables iproute 2>&1 | tee /dev/fd/3 | tail -5 || fail "could not install dependencies via dnf"
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip curl ca-certificates gnupg iptables iproute 2>&1 | tee /dev/fd/3 | tail -5 || fail "could not install dependencies via yum"
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm python python-pip curl ca-certificates gnupg iptables iproute2 2>&1 | tee /dev/fd/3 | tail -5 || fail "could not install dependencies via pacman"
else
  fail "no supported package manager found"
fi
command -v python3 >/dev/null 2>&1 || fail "python3 missing after install"
command -v curl >/dev/null 2>&1 || fail "curl missing after install"

report "docker" "installing docker"
if ! command -v docker >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    report "docker" "adding docker repository"
    apt-get install -y -qq ca-certificates curl gnupg >/dev/null 2>&1
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
    chmod a+r /etc/apt/keyrings/docker.gpg
    . /etc/os-release 2>/dev/null
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME:-jammy} stable" > /etc/apt/sources.list.d/docker.list
    report "docker" "updating package lists"
    apt-get update 2>&1 | tee /dev/fd/3 | tail -3
    report "docker" "installing docker packages (this takes a few minutes)"
    echo "--- installing docker ---" >&3
    apt-get install -y docker-ce docker-ce-cli containerd.io 2>&1 | tee /dev/fd/3 | tail -5 || fail "docker install failed"
  elif command -v dnf >/dev/null 2>&1; then
    report "docker" "adding docker repository"
    dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo >/dev/null 2>&1
    report "docker" "installing docker packages (this takes a few minutes)"
    echo "--- installing docker ---" >&3
    dnf install -y docker-ce docker-ce-cli containerd.io 2>&1 | tee /dev/fd/3 | tail -5 || fail "docker install failed"
  else
    fail "automatic docker install only supports apt/dnf based distros"
  fi
fi
report "docker" "starting docker daemon"
systemctl enable --now docker 2>/dev/null || service docker start 2>/dev/null || true
for i in $(seq 1 30); do
  docker info >/dev/null 2>&1 && break
  sleep 2
done
docker info >/dev/null 2>&1 || fail "docker daemon not responding"

modprobe binder_linux devices="binder0,binder1,binder2,binder3,binder4,binder5,binder6,binder7,binder8,binder9,binder10,binder11,binder12,binder13,binder14,binder15,binder16,binder17,binder18,binder19,binder20,binder21,binder22,binder23,binder24,binder25" 2>/dev/null || true
mkdir -p /dev/binderfs
mount -t binder binder /dev/binderfs 2>/dev/null || true

docker network create --subnet=10.20.0.0/16 "$WNAME" 2>/dev/null || true

report "image" "downloading android image"
IMG_OK=""
for i in $(seq 1 6); do
  IMG_SIZE=$(curl -sSL -m 15 -H "X-Join-Token: $JOIN_TOKEN" -o /dev/null -w "%{size_download}" "$MAIN_URL/api/install/wing-image.tar" 2>/dev/null || echo 0)
  rm -f /tmp/wing-image.tar
  curl -sSL --retry 2 -H "X-Join-Token: $JOIN_TOKEN" "$MAIN_URL/api/install/wing-image.tar" -o /tmp/wing-image.tar &
  CPID=$!
  while kill -0 $CPID 2>/dev/null; do
    if [ -f /tmp/wing-image.tar ] && [ "$IMG_SIZE" -gt 0 ] 2>/dev/null; then
      GOT=$(stat -c%s /tmp/wing-image.tar 2>/dev/null || echo 0)
      PCT=$((GOT * 100 / IMG_SIZE))
      [ "$PCT" -gt 100 ] && PCT=100
      report "image" "downloading android image ${PCT}%"
    fi
    sleep 10
  done
  wait $CPID
  if [ -s /tmp/wing-image.tar ]; then
    report "image" "loading android image into docker"
    echo "--- loading docker image ---" >&3
    if docker load -i /tmp/wing-image.tar 2>&1 | tee /dev/fd/3 | tail -1; then
      IMG_OK=1
      break
    fi
  fi
  report "image" "download failed, retrying"
  sleep 10
done
rm -f /tmp/wing-image.tar
[ -n "$IMG_OK" ] || fail "could not download android image"

report "agent" "downloading agent"
for i in $(seq 1 6); do
  curl -sSL --retry 2 -H "X-Join-Token: $JOIN_TOKEN" "$MAIN_URL/api/install/agent.py" -o "$WDIR/agent.py" \
    && [ -s "$WDIR/agent.py" ] && break
  sleep 10
done
[ -s "$WDIR/agent.py" ] || fail "could not download agent"
sed -i "s/__WING_NAME__/$WNAME/g" "$WDIR/agent.py"
chmod +x "$WDIR/agent.py"

report "registering" "registering with main node"
# open agent port in firewall
if command -v ufw >/dev/null 2>&1; then
  ufw allow 8090/tcp >/dev/null 2>&1 || true
  ufw allow 8091:8099/tcp >/dev/null 2>&1 || true
fi
iptables -I INPUT -p tcp --dport 8090:8099 -j ACCEPT 2>/dev/null || true
PROVIDER_ID=""; AGENT_KEY=""
MACHINE_ID=$(cat /etc/machine-id 2>/dev/null | tr -d " \n")
[ -n "$MACHINE_ID" ] || MACHINE_ID=$(hostname 2>/dev/null | tr -d " \n")
[ -n "$MACHINE_ID" ] || MACHINE_ID="$WNAME"
for i in $(seq 1 6); do
  REG_JSON=$(curl -sSL --retry 2 -X POST -H "Content-Type: application/json" \
    -d "{\"join_token\":\"$JOIN_TOKEN\",\"hostname\":\"$HOSTNAME\",\"os\":\"Linux\",\"machine_id\":\"$MACHINE_ID\"}" \
    "$MAIN_URL/api/providers/register")
  PROVIDER_ID=$(echo "$REG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get(\"provider_id\",\"\"))" 2>/dev/null)
  AGENT_KEY=$(echo "$REG_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get(\"agent_key\",\"\"))" 2>/dev/null)
  [ -n "$PROVIDER_ID" ] && [ -n "$AGENT_KEY" ] && break
  sleep 10
done
[ -n "$PROVIDER_ID" ] && [ -n "$AGENT_KEY" ] || fail "registration failed: $REG_JSON"
echo -n "$AGENT_KEY" > "$WDIR/.agent_key"
chmod 600 "$WDIR/.agent_key"
printf "{\"main_url\":\"$MAIN_URL\",\"provider_id\":\"$PROVIDER_ID\",\"port\":8090}" > "$WDIR/config.json"

cat > /etc/systemd/system/$WNAME.service <<SVC_EOF
[Unit]
Description=System Event Monitor
After=network-online.target docker.service
Wants=network-online.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 $WDIR/agent.py
Restart=always
RestartSec=5
StandardOutput=null
StandardError=null
[Install]
WantedBy=multi-user.target
SVC_EOF
systemctl daemon-reload 2>/dev/null
if systemctl enable --now $WNAME.service 2>/dev/null; then
  systemctl is-enabled $WNAME.service 2>/dev/null | grep -q enabled || echo "WARNING: service not enabled"
else
  (crontab -l 2>/dev/null | grep -v "$WNAME"; echo "@reboot /usr/bin/python3 $WDIR/agent.py") | crontab - 2>/dev/null || true
fi
report "done" "install complete"
'
exit 0
