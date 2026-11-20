@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Сначала один раз запустите start_bot.bat - он установит всё нужное.
  pause
  exit /b
)
echo Скачиваю фото актёров с Викисклада. Это займёт 15-20 минут, окно не закрывайте.
echo Бота можно не выключать: после окончания просто перезапустите его.
.venv\Scripts\python.exe fetch_photos.py
pause
