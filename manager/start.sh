#!/bin/bash
# Start the DroidFarm manager. Set DROIDFARM_PASSWORD before running.
DIR="$(cd "$(dirname "$0")" && pwd)"
export DROIDFARM_DATA="${DROIDFARM_DATA:-/root/droidfarm}"
export DROIDFARM_PORT="${DROIDFARM_PORT:-8080}"
mkdir -p "$DROIDFARM_DATA"
exec python3 "$DIR/app.py"
