FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates \
        lxc iproute2 iptables kmod \
        android-tools-adb dbus \
        weston pulseaudio \
        python3 dnsmasq procps iproute2 && \
    curl -s https://repo.waydro.id | bash && \
    apt-get install -y --no-install-recommends waydroid && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
COPY roblox-keepalive.sh /opt/roblox-keepalive.sh
COPY roblox-base.apk /opt/roblox-base.apk
COPY roblox-x86_64.apk /opt/roblox-x86_64.apk
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
