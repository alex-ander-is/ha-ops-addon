#!/usr/bin/with-contenv bashio
set -euo pipefail

mkdir -p /data/releases
mkdir -p /data/work
mkdir -p /app

exec python3 /app/server.py
