"""
Обновление бота из скачанного архива (нужно в первую очередь на Mac: «Архиватор» не
заменяет файлы, а создаёт новую папку «wingsquizbot 2»).

Что делает:
  1. Ищет в «Загрузках» и на рабочем столе самый свежий архив wingsquizbot*.zip
     или уже распакованную папку с quiz_bot.py (новее, чем у текущего бота).
  2. Копирует файлы из неё в папку бота, заменяя старые. Память (state.json), настройки
     (.env), скачанные вопросы и фото не трогает — их в архиве нет.
  3. Если бот был запущен — перезапускает его (на Mac).

Запуск: двойной щелчок по update.command (Mac) или update.bat (Windows).
"""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
KEEP = {"state.json", ".env", "questions.json", "fetch_state.json", "actors_photos.json",
        "photos_cache.json", "bot.log"}            # эти файлы никогда не заменяем
SKIP_DIRS = {".venv", "__pycache__"}


def say(msg: str) -> None:
    print(msg, flush=True)


def search_places() -> list[Path]:
    home = Path.home()
    places = [home / "Downloads", home / "Загрузки", home / "Desktop", home / "Рабочий стол",
              BOT_DIR.parent]
    return [p for p in places if p.is_dir()]


def find_update() -> tuple[str, Path, float] | None:
    """Самый свежий архив или распакованная папка с ботом (новее текущего quiz_bot.py)."""
    mine = (BOT_DIR / "quiz_bot.py").stat().st_mtime if (BOT_DIR / "quiz_bot.py").exists() else 0
    found = []
    for place in search_places():
        for z in place.glob("wingsquizbot*.zip"):
            try:
                with zipfile.ZipFile(z) as zf:
                    info = next(i for i in zf.infolist() if i.filename.endswith("quiz_bot.py"))
                found.append(("zip", z, time.mktime(info.date_time + (0, 0, -1))))
            except (zipfile.BadZipFile, StopIteration, OSError):
                continue
        # распакованные копии: .../wingsquizbot 2/wingsquizbot/quiz_bot.py и т.п. (до 3 уровней)
        for depth in ("*/quiz_bot.py", "*/*/quiz_bot.py", "*/*/*/quiz_bot.py"):
            for f in place.glob(depth):
                d = f.parent
                if d.resolve() == BOT_DIR or BOT_DIR in d.resolve().parents or ".venv" in d.parts:
                    continue
                if "wingsquizbot" in str(d).lower():
                    found.append(("folder", d, f.stat().st_mtime))
    found = [f for f in found if f[2] > mine + 60]   # только то, что новее текущей версии
    return max(found, key=lambda f: f[2]) if found else None


def unpack(zip_path: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="wingsquizbot-update-"))
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                continue  # на всякий случай — только файлы внутри архива
            z.extract(info, tmp)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(tmp / name, mode)
            if not info.is_dir():          # дата сборки, а не распаковки — по ней сравниваем версии
                ts = time.mktime(info.date_time + (0, 0, -1))
                os.utime(tmp / name, (ts, ts))
    src = next(tmp.rglob("quiz_bot.py"), None)
    if not src:
        raise RuntimeError("в архиве нет quiz_bot.py — это не архив бота")
    return src.parent


def copy_over(src: Path, dest: Path | None = None) -> int:
    dest = dest or BOT_DIR
    n = 0
    for f in src.rglob("*"):
        rel = f.relative_to(src)
        if f.is_dir() or rel.parts[0] in SKIP_DIRS or (len(rel.parts) == 1 and rel.name in KEEP):
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
        n += 1
    return n


def running_bot_pids() -> list[int]:
    if platform.system() == "Windows":
        return []
    try:
        # только сам бот: «…/python quiz_bot.py» (так его запускает start_bot), а не, например,
        # редактор, в котором открыт этот файл
        out = subprocess.run(["pgrep", "-f", r"[Pp]ython[0-9.]* ([^ ]*/)?quiz_bot\.py$"],
                             capture_output=True, text=True).stdout
    except FileNotFoundError:
        return []
    return [int(p) for p in out.split() if p.strip().isdigit() and int(p) != os.getpid()]


def is_installed(d: Path) -> bool:
    """Рабочая папка бота: там уже запускали бота (есть .venv или state.json)."""
    return (d / ".venv").exists() or (d / "state.json").exists()


def install_from_here() -> int:
    """Запуск из только что распакованной копии: обновить ею рабочую папку бота."""
    say("Похоже, это новая распакованная копия, а не рабочая папка бота.")
    say("Перетащите в это окно папку, где у вас работает бот (где лежит start_bot), и нажмите Enter:")
    raw = input("> ").strip()
    target = Path(raw.strip("'\"").replace("\\ ", " ")).expanduser()
    if not (target / "quiz_bot.py").exists():
        say(f"В папке {target} нет quiz_bot.py — это не папка бота. Ничего не изменено.")
        return 1
    if target.resolve() == BOT_DIR:
        say("Это та же самая папка. Ничего не изменено.")
        return 1
    pids = running_bot_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if pids:
        say("Останавливаю бота…")
        time.sleep(5)
    n = copy_over(BOT_DIR, target)
    say(f"Готово: в {target} обновлено файлов — {n}. Память, настройки и скачанные вопросы не тронуты.")
    say(f"Эту распакованную копию ({BOT_DIR}) теперь можно удалить.")
    say("Дальше для обновлений запускайте update.command из папки бота.")
    if pids and platform.system() == "Darwin":
        subprocess.run(["open", str(target / "start_bot.command")])
        say("Бот перезапущен в новом окне.")
    return 0


def main() -> int:
    if not is_installed(BOT_DIR):
        return install_from_here()
    say(f"Папка бота: {BOT_DIR}")
    upd = find_update()
    if not upd:
        say("Новее текущей версии ничего не нашёл. Если вы скачали новый архив — проверьте, что он "
            "лежит в «Загрузках» или на рабочем столе, и запустите меня снова.")
        return 1
    kind, path, _ = upd
    say(f"Нашёл обновление: {path}")
    src = unpack(path) if kind == "zip" else path
    pids = running_bot_pids()
    for pid in pids:                      # бот сохранит память и остановится
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if pids:
        say("Останавливаю бота…")
        time.sleep(5)
    n = copy_over(src)
    say(f"Готово: обновлено файлов — {n}. Память, настройки и скачанные вопросы не тронуты.")
    if kind == "zip":
        shutil.rmtree(src.parent if src.parent.name.startswith("wingsquizbot-update-") else src,
                      ignore_errors=True)
    else:
        say(f"Распакованную копию можно удалить: {path.parent if path.name == 'wingsquizbot' else path}")
    if pids and platform.system() == "Darwin":
        say("Запускаю бота снова в новом окне…")
        subprocess.run(["open", str(BOT_DIR / "start_bot.command")])
    elif pids:
        say("Запустите бота снова.")
    else:
        say("Бот не был запущен — запустите его, когда будет нужно (start_bot).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        say(f"Ошибка обновления: {e}")
        sys.exit(1)
