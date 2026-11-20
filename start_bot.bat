@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem --- ищем НАСТОЯЩИЙ Python (заглушка из Microsoft Store не подходит) ---
call :find_python
if not defined PY (
  echo Python не установлен. Устанавливаю Python 3.12, это займёт пару минут...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements --override "/passive InstallAllUsers=0 PrependPath=1 Include_launcher=1"
  call :find_python
)
if not defined PY (
  echo.
  echo Не получилось установить Python автоматически. Установите его вручную:
  echo   1. Откройте https://www.python.org/downloads/ и нажмите Download Python.
  echo   2. В установщике ОБЯЗАТЕЛЬНО поставьте галочку "Add python.exe to PATH".
  echo   3. После установки снова дважды щёлкните start_bot.bat
  pause
  exit /b
)

rem --- окружение с библиотеками бота ---
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys" >nul 2>&1 || rmdir /s /q .venv
)
if not exist .venv\Scripts\python.exe (
  echo Первый запуск: устанавливаю нужные библиотеки, подождите...
  %PY% -m venv .venv
  if not exist .venv\Scripts\python.exe (
    echo.
    echo Не удалось подготовить папку .venv. Пришлите, пожалуйста, текст этого окна.
    pause
    exit /b
  )
  .venv\Scripts\python.exe -m pip install --upgrade pip
)
echo Проверяю библиотеки...
.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo Не удалось установить библиотеки. Проверьте интернет и запустите start_bot.bat ещё раз.
  pause
  exit /b
)
echo Бот запускается. Не закрывайте это окно, пока бот нужен.
.venv\Scripts\python.exe quiz_bot.py
pause
exit /b

:find_python
set "PY="
for %%V in (313 312 311 310) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe""
)
if not defined PY py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
exit /b
