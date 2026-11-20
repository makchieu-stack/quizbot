#!/usr/bin/env bash
# Запуск бота на macOS / Linux: двойной щелчок или команда ./start_bot.sh
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python не найден, устанавливаю..."
  if [ "$(uname)" = "Darwin" ]; then
    command -v brew >/dev/null || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    brew install python
  else
    sudo apt-get update && sudo apt-get install -y python3 python3-venv
  fi
fi
if [ ! -x .venv/bin/python ]; then
  echo "Первый запуск: устанавливаю нужные библиотеки, подождите..."
  python3 -m venv .venv && .venv/bin/python -m pip install -q --upgrade pip
fi
echo "Проверяю библиотеки..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
echo "Бот запускается. Не закрывайте это окно, пока бот нужен."
exec .venv/bin/python quiz_bot.py
