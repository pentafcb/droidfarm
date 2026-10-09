#!/bin/bash
# Trim Docker container logs to last 1GB, runs hourly via cron
# Installed on VPS: /usr/local/bin/trim-docker-logs.sh
# Cron: 0 * * * * /usr/local/bin/trim-docker-logs.sh
for log in /var/lib/docker/containers/*/*-json.log; do
    [ -f "$log" ] || continue
    size=$(stat -c%s "$log" 2>/dev/null || echo 0)
    if [ "$size" -gt 1073741824 ]; then
        # Keep last 1GB
        tail -c 1073741824 "$log" > "$log.tmp" 2>/dev/null && mv "$log.tmp" "$log"
        echo "$(date): trimmed $log from $size to 1GB" >> /var/log/docker-log-trim.log
    fi
done
