#!/bin/bash
# NexPlay Tournament Bot — Auto-restart production runner
set -e
cd "$(dirname "$0")"

# Load secrets
if [ -f .agents/.env ]; then
    source .agents/.env
elif [ -f .env ]; then
    source .env
fi

echo "[$(date)] NexPlay Bot STARTING..."

while true; do
    echo "[$(date)] Starting process..."
    python3 -u main.py
    EXIT=$?
    echo "[$(date)] Exited (code $EXIT). Restarting in 5s..."
    sleep 5
done
