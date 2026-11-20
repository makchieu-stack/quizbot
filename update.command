#!/usr/bin/env bash
# Обновить бота из скачанного архива (двойной щелчок на Mac)
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
"$PY" update_bot.py
echo
read -n 1 -s -r -p "Нажмите любую клавишу, чтобы закрыть окно"
