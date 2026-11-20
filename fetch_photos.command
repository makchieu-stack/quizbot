#!/usr/bin/env bash
# Скачать фото актёров (macOS / Linux)
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Сначала один раз запустите start_bot.sh"; exit 1; }
.venv/bin/python fetch_photos.py
