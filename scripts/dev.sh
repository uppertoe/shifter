#!/usr/bin/env sh
# Boot shifter in dev mode (auth bypassed). DO NOT use in production.
set -e
cd "$(dirname "$0")/.."
mkdir -p data
exec env \
    PYTHONPATH=src \
    DEV_MODE=true \
    DEV_USER=alex \
    API_KEY= \
    DATABASE_PATH="$(pwd)/data/shifter-dev.db" \
    SCREENSHOT_DIR="$(pwd)/data/screenshots-dev" \
    TZ=Australia/Melbourne \
    .venv/bin/uvicorn shifter.main:app --reload --host 127.0.0.1 --port "${PORT:-8765}"
