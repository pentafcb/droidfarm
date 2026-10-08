#!/bin/bash
# Start the Jerusalem 1.0 manager. Set JERUSALEM_PASSWORD before running.
DIR="$(cd "$(dirname "$0")" && pwd)"
export JERUSALEM_DATA="${JERUSALEM_DATA:-/root/jerusalem}"
export JERUSALEM_PORT="${JERUSALEM_PORT:-8080}"
mkdir -p "$JERUSALEM_DATA"
exec python3 "$DIR/app.py"
