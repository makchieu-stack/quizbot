"""
Telegram-квиз-бот @wingsquizbot. Вопросы — база «Что? Где? Когда?» с https://gotquestions.online/
(лицензия CC BY-NC-ND: тексты публикуются без изменений, с указанием автора, пакета и ссылки;
бот некоммерческий).

Команды:
  /quiz_start [N]      — квиз из N вопросов (управлять квизом может любой участник;
                         ADMINS_ONLY=1 в .env — только админы группы)
  /quiz_stop           — остановить квиз
  /offer on|off        — бот раз в день сам предлагает сыграть квиз (в группах, по умолчанию вкл.)
  /q                   — вопрос прямо сейчас
  /answer              — показать ответ
  /top                 — таблица лидеров
  /help                — помощь
"""
from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta
import json
import logging
import os
import random
import sys
import time
from functools import wraps
from pathlib import Path

import requests

from dotenv import load_dotenv
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument,
                      InputMediaPhoto, Update)
from telegram.constants import ChatType, ParseMode
from telegram.error import Conflict, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler,
                          filters)

from answer_check import is_correct

HERE = Path(__file__).resolve().parent
QUESTIONS_FILE = HERE / "questions.json"
STATE_FILE = HERE / "state.json"
ANSWER_TIMEOUT = 10 * 60          # через сколько секунд бот сам показывает ответ на /q
OFFER_TTL = 60 * 60               # кнопка «Начать квиз» в предложении действует час
OFFER_DEFAULT_HOURS = (19, 20, 21)  # пока в чате мало истории — предлагаем вечером
QUIZ_STEP = 5 * 60                # в квизе: на вопрос 5 минут; после верного ответа — сразу следующий
NEXT_AFTER_CORRECT = 5            # секунд между верным ответом и следующим вопросом квиза
MOVIE_QUESTION_NO = 4             # в квизе «все темы» вопросы про кино идут подряд с 4-го:
DEFAULT_QUIZ_SIZE = 10            # вопросов в квизе по умолчанию
MAX_QUIZ_SIZE = 50
APPEAL_WINDOW = 24 * 3600         # апелляцию можно подать в течение суток после ответа
STARTED_AT = time.time()          # когда бот запущен: команды, отправленные раньше, не выполняем
Q_COOLDOWN = 60                   # /q не чаще, чем раз в минуту (пока на вопрос никто не ответил)
FETCH_PACKS = 5                   # сколько пакетов подкачивать, когда вопросы кончились
RESERVE_TARGET = 100              # сколько незаданных вопросов держать про запас в каждом чате
REFRESH_EVERY = 24 * 3600         # как часто пытаться обновлять базу
REFRESH_HOUR = 5                  # в котором часу (по часам компьютера) обновлять базу

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(HERE / "bot.log", encoding="utf-8")],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("quizbot")

HELP = (
    "<b>Квиз «Что? Где? Когда?»</b>\n"
    "Вопросы ЧГК из баз gotquestions.online и db.chgk.info, "
    "а также «Угадай фильм по актёрам».\n\n"
    "/q — вопрос прямо сейчас (/q кино, /q чгк — из нужной темы)\n"
    "/answer — показать ответ\n"
    "/top — таблица лидеров\n"
    f"/quiz_start 10 — квиз из 10 вопросов (на каждый — до {QUIZ_STEP // 60} минут)\n"
    "/quiz_stop — остановить квиз\n"
    "/offer — раз в день бот сам предлагает сыграть квиз, обычно в то время, когда "
    "в чате чаще всего играют (/offer off — выключить, /offer on — включить)\n"
    "/theme — тема вопросов чата: чгк, кино (угадай фильм по актёрам) или все\n"
    "/appeal — апелляция: ответьте этой командой на сообщение игрока с ответом, "
    "если бот зря его не засчитал (решает админ или 3 голоса участников)\n"
    "\n"
    "Чтобы ответить, нажмите «Ответить» (reply) на сообщение бота с вопросом — "
    "остальные сообщения бот не читает, так что обсуждать вопрос можно свободно "
    "(в личке с ботом можно писать ответ просто так). На неверный ответ бот ставит ❌. Регистр, ё/е, кавычки и знаки препинания не важны, "
    "небольшие опечатки прощаются. Первый правильно ответивший получает очко. "
    "В квизе после правильного ответа сразу идёт следующий вопрос. "
    f"Если никто не ответит, бот покажет ответ сам: в квизе — через {QUIZ_STEP // 60} мин, "
    f"/q — через {ANSWER_TIMEOUT // 60} мин.\n\n"
    "Бот некоммерческий. Вопросы публикуются без изменений по лицензии CC BY-NC-ND, "
    "с указанием автора, пакета и ссылки на вопрос."
)


# ------------------------------------------------------------------ хранение

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


THEMES = {
    "chgk": ("ЧГК", ("чгк", "chgk", "что-где-когда")),
    "movies": ("Кино: угадай фильм по актёрам", ("кино", "фильм", "фильмы", "movies", "movie")),
}
ALL_WORDS = ("все", "всё", "all", "микс", "любые")


def parse_theme(word: str) -> str | None:
    """«кино» -> "movies", «чгк» -> "chgk", «все» -> "all", иначе None."""
    w = word.strip().lower()
    if w in ALL_WORDS:
        return "all"
    for key, (_, words) in THEMES.items():
        if w in words:
            return key
    return None


def theme_name(theme: str) -> str:
    return "все темы" if theme == "all" else THEMES.get(theme, (theme,))[0]


def split_theme(args: list[str]) -> tuple[list[str], str | None]:
    """Выделить тему из аргументов команды: (остальные аргументы, тема или None)."""
    rest, theme = [], None
    for a in args:
        t = parse_theme(a)
        if t and theme is None:
            theme = t
        else:
            rest.append(a)
    return rest, theme


def merge_seed() -> None:
    """Стартовый набор из архива (questions_seed.json, fetch_state_seed.json) добавляется
    к вашей базе, а не заменяет её — обновление бота ничего не затирает."""
    seed = load_json(HERE / "questions_seed.json", None)
    if seed:
        db = load_json(QUESTIONS_FILE, {"questions": []})
        have = {q["id"] for q in db["questions"]}
        new = [q for q in seed.get("questions", []) if q["id"] not in have]
        if new or not QUESTIONS_FILE.exists():
            db["questions"].extend(new)
            save_json(QUESTIONS_FILE, db)
            log.info("Из стартового набора добавлено вопросов: %d", len(new))
    seed_state = load_json(HERE / "fetch_state_seed.json", None)
    if seed_state:
        fs = load_json(HERE / "fetch_state.json", {"seen_packs": [], "max_pack_id": 0})
        fs["seen_packs"] = sorted(set(fs.get("seen_packs", [])) | set(seed_state.get("seen_packs", [])))
        fs["max_pack_id"] = max(fs.get("max_pack_id", 0), seed_state.get("max_pack_id", 0))
        save_json(HERE / "fetch_state.json", fs)


class Store:
    def __init__(self) -> None:
        self.state = load_json(STATE_FILE, {"chats": {}})
        self.dirty = False
        self.questions: dict[str, dict] = {}
        self.reload_questions()
        self.locks: dict[str, asyncio.Lock] = {}
        self.fetch_lock = asyncio.Lock()

    def reload_questions(self) -> None:
        data = load_json(QUESTIONS_FILE, {"questions": []})
        qs = data.get("questions", [])
        # свои тематические базы: custom_*.json (например, custom_movies.json)
        for f in sorted(HERE.glob("custom_*.json")):
            qs += load_json(f, {"questions": []}).get("questions", [])
        # фото актёров (fetch_photos.py): вопрос о кино задаём, только если есть фото всех актёров
        photos = load_json(HERE / "actors_photos.json", {})
        kept = []
        for q in qs:
            q.setdefault("category", "chgk")
            if q["category"] == "movies":
                ph = photos.get(q["id"])
                if not ph or [p["actor"] for p in ph] != q.get("actors", []) or \
                        not all((HERE / p["file"]).exists() for p in ph):
                    continue
                q["photos"] = ph
            kept.append(q)
        qs = kept
        self.questions = {q["id"]: q for q in qs}
        counts = {}
        for q in qs:
            counts[q["category"]] = counts.get(q["category"], 0) + 1
        log.info("В базе вопросов: %d (%s)", len(self.questions),
                 ", ".join(f"{theme_name(k)}: {v}" for k, v in counts.items()))

    def chat(self, chat_id: int) -> dict:
        c = self.state["chats"].setdefault(str(chat_id), {})
        c.pop("interval", None)
        c.pop("last_post", None)
        c.setdefault("quiz", None)    # {"total", "asked", "next_at", "scores"}
        c.pop("daily", None)          # вопросы дня убраны — старые настройки не нужны
        c.setdefault("quiz_history", [])  # когда в чате запускали квиз (для предложений)
        c.setdefault("offer", {"enabled": int(chat_id) < 0})  # предложения — только в группах
        c.setdefault("used", [])
        c.setdefault("current", None)
        c.setdefault("scores", {})
        c.setdefault("attempts", {})  # id сообщения с ответом -> {uid, name, text, qid, ts, ok}
        c.setdefault("credited", [])  # "qid:uid" — кому уже засчитан вопрос
        c.setdefault("appeals", {})   # номер апелляции -> {...}
        c.setdefault("appeal_seq", 0)
        c.setdefault("theme", "all")  # тема по умолчанию для /q, квиза и вопросов дня
        return c

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self.locks.setdefault(str(chat_id), asyncio.Lock())

    def save(self) -> None:
        self.state["saved_at"] = time.time()
        self.dirty = True
        save_json(STATE_FILE, self.state)

    def unused(self, chat_id: int, theme: str = "all") -> list[str]:
        used = set(self.chat(chat_id)["used"])
        return [qid for qid, q in self.questions.items()
                if qid not in used and (theme == "all" or q["category"] == theme)]


store: Store  # создаётся в main()


# ------------------------------------------------------------------ оформление

def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def question_html(q: dict) -> str:
    text = "Угадайте фильм по этим актёрам." if q.get("photos") else q["text"]
    parts = [f"❓ <b>Вопрос</b>\n\n{esc(text)}"]
    if q.get("razdatka"):
        parts.append(f"<i>Раздаточный материал:</i>\n{esc(q['razdatka'])}")
    parts.append("<i>Чтобы ответить, сделайте «Ответить» (reply) на это сообщение.</i>")
    return "\n\n".join(parts)


def answer_html(q: dict, header: str) -> str:
    lines = [header, "", f"<b>Ответ:</b> {esc(q['answer'])}"]
    if q.get("zachet"):
        lines.append(f"<b>Зачёт:</b> {esc(q['zachet'])}")
    if q.get("comment"):
        lines.append(f"<b>Комментарий:</b> {esc(q['comment'])}")
    if q.get("source"):
        lines.append(f"<b>Источник:</b> {esc(q['source'])}")
    lines.append("")
    site = q.get("site", "gotquestions.online")
    if site == "custom":
        if q.get("photos"):
            lines.append("<b>Актёры:</b> " + esc(", ".join(p["actor"] for p in q["photos"])))
            lines.append("")
            lines.append("Фото с Викисклада: " + "; ".join(
                f'<a href="{esc(p["page"])}">{esc(p["actor"])}</a> — {esc(p["author"][:80])}, {esc(p["license"])}'
                for p in q["photos"]))
        lines.append(f"Тема: «{esc(q.get('pack_title', ''))}». Вопрос составлен для этого бота.")
        return "\n".join(lines)
    lines.append(f"Автор: {esc(q.get('author') or 'не указан')}")
    label = "Пакет" if site == "gotquestions.online" else "Турнир"
    lines.append(f"{label}: «{esc(q.get('pack_title', ''))}»")
    lines.append(f"Вопрос на {site}: {esc(q['url'])}")
    return "\n".join(lines)


def split_text(text: str, limit: int = 4000) -> list[str]:
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


async def send_long(bot, chat_id: int, text: str) -> list[int]:
    ids = []
    for chunk in split_text(text):
        m = await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
        ids.append(m.message_id)
    return ids


# ------------------------------------------------------------------ вопросы

async def run_fetcher(packs: int) -> None:
    """Запускает fetch_questions.py (вызывать под store.fetch_lock) и перечитывает базу."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(HERE / "fetch_questions.py"), "--packs", str(packs),
            cwd=str(HERE), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15 * 60)
        log.info("fetch_questions.py:\n%s", out.decode("utf-8", "replace")[-2000:])
    except Exception:  # noqa: BLE001
        log.exception("Не удалось подкачать вопросы")
    store.reload_questions()


async def fetch_more(bot, chat_id: int) -> None:
    """Вопросы ЧГК в чате кончились — срочно подкачать."""
    async with store.fetch_lock:
        if store.unused(chat_id, "chgk"):
            return  # пока ждали, другой чат уже подкачал
        await bot.send_message(chat_id, "Вопросы закончились — подкачиваю новые (gotquestions.online, "
                                        "запасная база db.chgk.info), это займёт около минуты…")
        await run_fetcher(FETCH_PACKS)


def min_reserve() -> int:
    """Сколько незаданных вопросов ЧГК осталось в самом «прожорливом» чате (их докачиваем)."""
    chats = [int(k) for k in store.state["chats"]]
    return min((len(store.unused(c, "chgk")) for c in chats),
               default=len([q for q in store.questions.values() if q["category"] == "chgk"]))


async def refresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в день: докачать вопросы, чтобы в запасе было не меньше RESERVE_TARGET."""
    async with store.fetch_lock:
        reserve = min_reserve()
        # 1 пакет для свежести, больше — если запас меньше нормы (в пакете обычно 30–36 вопросов)
        packs = 1 if reserve >= RESERVE_TARGET else min(FETCH_PACKS, 1 + (RESERVE_TARGET - reserve) // 25)
        log.info("Ежедневное обновление базы: в запасе %d, качаю пакетов: %d", reserve, packs)
        await run_fetcher(packs)
        log.info("После обновления в запасе: %d", min_reserve())
    store.state["last_refresh"] = time.time()
    store.save()
    schedule_refresh(context.application)


def schedule_refresh(app: Application) -> None:
    """Следующее обновление — завтра в REFRESH_HOUR; если давно не обновлялись — через минуту."""
    last = store.state.get("last_refresh", 0)
    if time.time() - last >= REFRESH_EVERY:
        when = time.time() + 60
    else:
        nxt = datetime.fromtimestamp(last).astimezone().replace(hour=REFRESH_HOUR, minute=0, second=0)
        nxt += timedelta(days=1)
        when = max(nxt.timestamp(), last + REFRESH_EVERY / 2)
    for j in app.job_queue.get_jobs_by_name("refresh"):
        j.schedule_removal()
    app.job_queue.run_once(refresh_job, max(1, when - time.time()), name="refresh")
    log.info("Следующее обновление базы вопросов: %s", fmt_ts(when))


def movies_share() -> float:
    """Доля вопросов о кино в режиме «все темы» (MOVIES_SHARE в .env, по умолчанию 10%)."""
    try:
        v = float(os.getenv("MOVIES_SHARE", "10").replace(",", ".").rstrip("%"))
    except ValueError:
        v = 10
    return min(max(v / 100 if v > 1 else v, 0.0), 1.0)


async def pick_question(bot, chat_id: int, theme: str = "all") -> dict | None:
    if theme == "all":
        # сначала жребий: кино с вероятностью MOVIES_SHARE, иначе ЧГК
        has = {cat for cat in (q["category"] for q in store.questions.values())}
        if "movies" in has and "chgk" in has:
            theme = "movies" if random.random() < movies_share() else "chgk"
    if not store.unused(chat_id, theme) and theme in ("all", "chgk"):
        await fetch_more(bot, chat_id)   # докачать можно только ЧГК
    pool = store.unused(chat_id, theme)
    if not pool:
        in_theme = [qid for qid, q in store.questions.items()
                    if theme == "all" or q["category"] == theme]
        if not in_theme:
            return None
        # новых взять неоткуда — начинаем эту тему заново
        c = store.chat(chat_id)
        c["used"] = [qid for qid in c["used"] if qid not in set(in_theme)]
        await bot.send_message(chat_id, f"Новых вопросов ({theme_name(theme)}) пока нет, "
                                        "начинаем по второму кругу.")
        pool = in_theme
    return store.questions[random.choice(pool)]


async def reveal(bot, chat_id: int, header: str) -> None:
    """Показать ответ на текущий вопрос и закрыть его (вызывать под замком чата)."""
    c = store.chat(chat_id)
    cur = c["current"]
    if not cur:
        return
    c["current"] = None
    store.save()
    q = store.questions.get(cur["qid"])
    if q:
        await send_long(bot, chat_id, answer_html(q, header))


async def post_question(app: Application, chat_id: int, title: str = "Вопрос",
                        timeout: float = ANSWER_TIMEOUT, theme: str | None = None) -> bool:
    """Отправить новый вопрос (вызывать под замком чата). True — если отправлен."""
    bot = app.bot
    c = store.chat(chat_id)
    if c["current"]:
        await reveal(bot, chat_id, "⏭ Ответ на предыдущий вопрос:")
    q = await pick_question(bot, chat_id, theme or c.get("theme", "all"))
    if q is None:
        if (theme or c.get("theme")) == "movies":
            await bot.send_message(chat_id, "Вопросов о кино пока нет: на компьютере с ботом нужно один раз "
                                            "запустить fetch_photos.bat, чтобы скачать фото актёров.")
        else:
            await bot.send_message(chat_id, "В базе нет вопросов и скачать их не удалось. "
                                            "Попробуйте позже.")
        return False
    text = question_html(q).replace("<b>Вопрос</b>", f"<b>{esc(title)}</b>", 1)
    img = HERE / q["image"] if q.get("image") else None
    if q.get("photos"):
        files = [(HERE / p["file"]).open("rb") for p in q["photos"]]
        try:
            media = [InputMediaPhoto(f, caption=text if i == 0 else None,
                                     parse_mode=ParseMode.HTML if i == 0 else None)
                     for i, f in enumerate(files)]
            msgs = await bot.send_media_group(chat_id, media)
        finally:
            for f in files:
                f.close()
        msg_ids = [m.message_id for m in msgs]  # реплай на любое из фото засчитывается
    elif img and img.exists():
        with img.open("rb") as f:
            if len(text) <= 1000:
                m = await bot.send_photo(chat_id, f, caption=text, parse_mode=ParseMode.HTML)
                msg_ids = [m.message_id]
            else:
                m = await bot.send_photo(chat_id, f)
                msg_ids = [m.message_id] + await send_long(bot, chat_id, text)
    else:
        msg_ids = await send_long(bot, chat_id, text)
    c["used"].append(q["id"])
    c["current"] = {"qid": q["id"], "asked_at": time.time(), "timeout": timeout,
                    "message_ids": msg_ids}
    store.save()
    schedule_timeout(app, chat_id, timeout)
    return True


def schedule_timeout(app: Application, chat_id: int, delay: float) -> None:
    name = f"timeout:{chat_id}"
    for j in app.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    qid = store.chat(chat_id)["current"]["qid"]
    app.job_queue.run_once(timeout_job, max(1, delay), chat_id=chat_id, name=name, data=qid)


async def timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    async with store.lock(chat_id):
        cur = store.chat(chat_id)["current"]
        if cur and cur["qid"] == context.job.data:
            mins = round(cur.get("timeout", ANSWER_TIMEOUT) / 60)
            await reveal(context.bot, chat_id, f"⏰ За {mins} мин никто не ответил.")


def run_at(app: Application, callback, chat_id: int, name: str, when_ts: float) -> None:
    """Один запуск задачи в момент when_ts (старая задача с тем же именем снимается)."""
    for j in app.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    app.job_queue.run_once(callback, max(1, when_ts - time.time()), chat_id=chat_id, name=name)


# ---------------- квиз из N вопросов

async def quiz_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    app, chat_id = context.application, context.job.chat_id
    async with store.lock(chat_id):
        c = store.chat(chat_id)
        quiz = c["quiz"]
        if not quiz:
            return
        if c["current"]:
            await reveal(app.bot, chat_id, "⏰ Время вышло.")
        if quiz["asked"] >= quiz["total"]:
            await finish_quiz(app.bot, chat_id)
            return
        n = quiz["asked"] + 1
        theme = quiz.get("theme") or c.get("theme", "all")
        if theme == "all":
            # квиз «все темы»: про кино — по одному на каждые 10 вопросов, подряд с 4-го
            # (10 → 4-й; 20 → 4-й и 5-й; 30 → 4–6-й), остальные — ЧГК (без жребия)
            has_movies = any(q["category"] == "movies" for q in store.questions.values())
            theme = "movies" if has_movies and n in movie_positions(quiz["total"]) else "chgk"
        ok = await post_question(app, chat_id, f"Вопрос {n} из {quiz['total']}", QUIZ_STEP, theme)
        if not ok:
            c["quiz"] = None
            store.save()
            return
        quiz["asked"] = n
        quiz["next_at"] = time.time() + QUIZ_STEP + 2
        store.save()
        run_at(app, quiz_tick, chat_id, f"quiz:{chat_id}", quiz["next_at"])


def movie_positions(total: int) -> range:
    """Номера вопросов о кино в квизе «все темы»: по одному на каждые 10, подряд с 4-го.
    В квизах на 4–9 вопросов — один (4-й)."""
    if total < MOVIE_QUESTION_NO:
        return range(0)
    k = max(1, total // 10)
    return range(MOVIE_QUESTION_NO, MOVIE_QUESTION_NO + k)


async def finish_quiz(bot, chat_id: int) -> None:
    c = store.chat(chat_id)
    quiz, c["quiz"] = c["quiz"], None
    store.save()
    lines = [f"🏁 <b>Квиз окончен!</b> Вопросов: {quiz['asked']}", ""]
    rows = sorted(quiz["scores"].values(), key=lambda s: -s["points"])
    if rows:
        medals = ["🥇", "🥈", "🥉"]
        for i, s in enumerate(rows[:10]):
            lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {esc(s['name'])} — {s['points']}")
    else:
        lines.append("Никто не ответил правильно — в следующий раз получится!")
    lines += ["", "Общая таблица — /top"]
    await bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------- предложение сыграть квиз (раз в день, в привычное для чата время)

def offer_hour_weights(c: dict) -> dict[int, float]:
    """Вес каждого часа суток по истории запусков квиза в чате (по часам этого компьютера)."""
    now = time.time()
    hours = [datetime.fromtimestamp(t).astimezone().hour
             for t in c.get("quiz_history", []) if now - t < 90 * 86400]
    if len(hours) < 3:
        return {h: 1.0 for h in OFFER_DEFAULT_HOURS}
    w: dict[int, float] = {}
    for h in hours:
        w[h] = w.get(h, 0) + 1.0
        for nb in ((h - 1) % 24, (h + 1) % 24):     # соседние часы тоже немного
            w[nb] = w.get(nb, 0) + 0.3
    return w


def quiz_played_on(c: dict, day) -> bool:
    return any(datetime.fromtimestamp(t).astimezone().date() == day for t in c.get("quiz_history", []))


def plan_offer(c: dict, after: float | None = None) -> float:
    """Выбрать время следующего предложения: случайный час с учётом привычек чата."""
    after = after or time.time()
    weights = offer_hour_weights(c)
    start_day = datetime.fromtimestamp(after).astimezone().date()
    for add in range(0, 3):
        day = start_day + timedelta(days=add)
        if day.isoformat() == c["offer"].get("day") or quiz_played_on(c, day):
            continue  # сегодня уже предлагали или уже играли
        slots = []
        for h, w in weights.items():
            for minute in range(0, 60, 5):
                ts = datetime(day.year, day.month, day.day, h, minute).astimezone().timestamp()
                if ts > after + 10 * 60:
                    slots.append((ts, w))
        if slots:
            ts = random.choices([t for t, _ in slots], [w for _, w in slots])[0]
            c["offer"]["next_at"] = ts + random.randint(0, 4) * 60
            return c["offer"]["next_at"]
    c["offer"]["next_at"] = after + 86400
    return c["offer"]["next_at"]


def offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"▶️ Начать квиз ({DEFAULT_QUIZ_SIZE} вопросов)", callback_data="offer:go"),
        InlineKeyboardButton("Не сегодня", callback_data="offer:no")]])


def schedule_offer(app: Application, chat_id: int, replan: bool = False) -> None:
    c = store.chat(chat_id)
    off = c["offer"]
    if not off.get("enabled"):
        for j in app.job_queue.get_jobs_by_name(f"offer:{chat_id}"):
            j.schedule_removal()
        return
    if replan or not off.get("next_at") or off["next_at"] < time.time() - 3600:
        plan_offer(c)
        store.save()
    run_at(app, offer_tick, chat_id, f"offer:{chat_id}", max(time.time() + 5, off["next_at"]))


def ensure_offer(app: Application, chat_id: int) -> None:
    """Новый чат: запланировать первое предложение, если ещё не запланировано."""
    c = store.chat(chat_id)
    if c["offer"].get("enabled") and not app.job_queue.get_jobs_by_name(f"offer:{chat_id}"):
        schedule_offer(app, chat_id)


async def offer_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    app, chat_id = context.application, context.job.chat_id
    async with store.lock(chat_id):
        c = store.chat(chat_id)
        off = c["offer"]
        if not off.get("enabled"):
            return
        today = datetime.now().astimezone().date()
        recent = any(time.time() - t < 3 * 3600 for t in c["quiz_history"])
        if c["quiz"] or recent or quiz_played_on(c, today):
            off["day"] = today.isoformat()          # сегодня уже играли — не надоедаем
        else:
            try:
                m = await app.bot.send_message(
                    chat_id, "🎯 Сыграем квиз? Нажмите кнопку — и начнём!",
                    reply_markup=offer_keyboard())
                off.update(day=today.isoformat(), msg_id=m.message_id, expires=time.time() + OFFER_TTL)
                run_at(app, offer_expire, chat_id, f"offer_exp:{chat_id}", off["expires"])
            except TelegramError as e:
                log.warning("Не удалось предложить квиз в чате %s: %s", chat_id, e)
                if "forbidden" in str(e).lower() or "not found" in str(e).lower() or "kicked" in str(e).lower():
                    off["enabled"] = False      # бота убрали из чата
                    store.save()
                    return
        plan_offer(c, time.time() + 60)
        store.save()
    run_at(app, offer_tick, chat_id, f"offer:{chat_id}", off["next_at"])


async def offer_expire(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    off = store.chat(chat_id)["offer"]
    if off.get("msg_id"):
        try:
            await context.bot.edit_message_text("🎯 Предложение сыграть квиз устарело. Начать — /quiz_start",
                                                chat_id=chat_id, message_id=off["msg_id"])
        except TelegramError:
            pass
        off["msg_id"] = None
        store.save()


async def on_offer_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    chat_id = update.effective_chat.id
    off = store.chat(chat_id)["offer"]
    if cq.message.message_id != off.get("msg_id") or time.time() > off.get("expires", 0):
        await cq.answer("Это предложение уже неактуально. Начать квиз — /quiz_start")
        return
    user = update.effective_user
    if cq.data == "offer:no":
        off["msg_id"] = None
        store.save()
        await cq.answer()
        await cq.edit_message_text(f"Хорошо, в другой раз 🙂 ({user.full_name})")
        return
    if not await is_admin(update, context):
        await cq.answer("Запускать квиз могут только администраторы чата.", show_alert=True)
        return
    err = await begin_quiz(context.application, chat_id, DEFAULT_QUIZ_SIZE, None)
    off["msg_id"] = None
    store.save()
    await cq.answer()
    await cq.edit_message_text(f"▶️ {user.full_name} запускает квиз!" if not err else err)


async def cmd_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat_id = update.effective_chat.id
    c = store.chat(chat_id)
    arg = (context.args or [""])[0].lower()
    if arg in ("on", "off", "вкл", "выкл"):
        if not await is_admin(update, context):
            await msg.reply_text("Менять эту настройку могут только администраторы чата.")
            return
        c["offer"]["enabled"] = arg in ("on", "вкл")
        store.save()
        schedule_offer(context.application, chat_id, replan=True)
    off = c["offer"]
    if off.get("enabled"):
        hours = offer_hour_weights(c)
        top = sorted(hours, key=lambda h: -hours[h])[:3]
        how = ("пока в чате мало истории, предлагаю вечером" if len(c["quiz_history"]) < 3 else
               "чаще всего около " + ", ".join(f"{h}:00" for h in sorted(top)) + " — когда у вас обычно играют")
        await msg.reply_text(f"🎯 Предложения сыграть квиз включены: раз в день, {how}. "
                             f"Ближайшее — {fmt_ts(off['next_at'])}.\nВыключить — /offer off")
    else:
        await msg.reply_text("Предложения сыграть квиз выключены. Включить — /offer on")


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%d.%m в %H:%M")


# ------------------------------------------------------------------ команды

def admins_only() -> bool:
    """ADMINS_ONLY=1 в .env — управлять квизом могут только админы группы. По умолчанию — все."""
    return os.getenv("ADMINS_ONLY", "0").strip().lower() in ("1", "true", "yes", "да")


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not admins_only() or chat.type == ChatType.PRIVATE:
        return True
    if update.effective_message.sender_chat and update.effective_message.sender_chat.id == chat.id:
        return True  # анонимный админ
    member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
    return member.status in ("administrator", "creator")


_told_offline: set[int] = set()


def fresh_only(handler):
    """Команды, отправленные, пока бот был выключен, не выполняем (иначе после включения
    бот разом выполнит всё накопившееся). Вместо этого один раз пишем в чат."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg and msg.date and msg.date.timestamp() < STARTED_AT - 5:
            chat_id = update.effective_chat.id
            log.info("Пропущена старая команда %r в чате %s", msg.text, chat_id)
            if chat_id not in _told_offline:
                _told_offline.add(chat_id)
                await context.bot.send_message(
                    chat_id, "😴 Я был выключен и пропустил команды, отправленные за это время. "
                             "Сейчас я снова работаю — повторите команду, если она ещё нужна.")
            return
        ensure_offer(context.application, update.effective_chat.id)
        await handler(update, context)
    return wrapper


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/help от %s в чате %s", update.effective_user.id if update.effective_user else "?",
             update.effective_chat.id)
    await update.effective_message.reply_html(HELP, disable_web_page_preview=True)


async def cmd_q(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    async with store.lock(chat_id):
        c = store.chat(chat_id)
        if c["quiz"]:
            await update.effective_message.reply_text(
                "Сейчас идёт квиз — следующий вопрос придёт сам. Остановить квиз — /quiz_stop")
            return
        if c["current"] and time.time() - c["current"]["asked_at"] < Q_COOLDOWN:
            await update.effective_message.reply_text(
                "Вопрос только что задан — ответьте на него реплаем или откройте ответ: /answer")
            return
        _, theme = split_theme(context.args or [])
        await post_question(context.application, chat_id, theme=theme)


async def cmd_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    async with store.lock(chat_id):
        if not store.chat(chat_id)["current"]:
            await update.effective_message.reply_text("Сейчас нет открытого вопроса. /q — новый вопрос.")
            return
        await reveal(context.bot, chat_id, "📖 Ответ:")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    scores = store.chat(update.effective_chat.id)["scores"]
    if not scores:
        await update.effective_message.reply_text("Пока никто не набрал очков. /q — первый вопрос!")
        return
    rows = sorted(scores.values(), key=lambda s: -s["points"])[:10]
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Таблица лидеров</b>", ""]
    for i, s in enumerate(rows):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {esc(s['name'])} — {s['points']}")
    await update.effective_message.reply_html("\n".join(lines))


async def begin_quiz(app: Application, chat_id: int, total: int, theme: str | None) -> str | None:
    """Запустить квиз. Возвращает текст ошибки или None."""
    async with store.lock(chat_id):
        c = store.chat(chat_id)
        if c["quiz"]:
            return "Квиз уже идёт. Остановить — /quiz_stop"
        theme = theme or c["theme"]
        c["quiz"] = {"total": total, "asked": 0, "scores": {}, "next_at": time.time() + 3,
                     "started": time.time(), "theme": theme}
        c["quiz_history"] = (c["quiz_history"] + [time.time()])[-50:]
        store.save()
    await app.bot.send_message(
        chat_id, f"🎯 Начинаем квиз из {total} вопр. (тема: {theme_name(theme)})! На каждый вопрос — "
                 f"до {QUIZ_STEP // 60} мин, после правильного ответа сразу следующий. Отвечайте реплаем "
                 "на сообщение с вопросом. Остановить — /quiz_stop")
    run_at(app, quiz_tick, chat_id, f"quiz:{chat_id}", time.time() + 3)
    return None


async def cmd_quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not await is_admin(update, context):
        await msg.reply_text("Запускать квиз могут только администраторы чата.")
        return
    total = DEFAULT_QUIZ_SIZE
    args, theme = split_theme(context.args or [])
    if args:
        try:
            total = int(args[0])
        except ValueError:
            await msg.reply_text("Укажите число вопросов и, если нужно, тему, например: "
                                 "/quiz_start 10 или /quiz_start 10 кино")
            return
    total = max(1, min(total, MAX_QUIZ_SIZE))
    err = await begin_quiz(context.application, update.effective_chat.id, total, theme)
    if err:
        await msg.reply_text(err)


async def cmd_quiz_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not await is_admin(update, context):
        await msg.reply_text("Останавливать квиз могут только администраторы чата.")
        return
    chat_id = update.effective_chat.id
    for j in context.job_queue.get_jobs_by_name(f"quiz:{chat_id}"):
        j.schedule_removal()
    async with store.lock(chat_id):
        c = store.chat(chat_id)
        if not c["quiz"]:
            await msg.reply_text("Квиз сейчас не идёт. Начать — /quiz_start 10")
            return
        if c["current"]:
            await reveal(context.bot, chat_id, "⏹ Квиз остановлен. Ответ на последний вопрос:")
        await finish_quiz(context.bot, chat_id)


THEME_USAGE = ("Темы вопросов:\n"
               "• чгк — «Что? Где? Когда?»\n"
               "• кино — угадай фильм по актёрам\n"
               "• все — вперемешку: в квизе про кино 1 вопрос на каждые 10 (с 4-го), в /q — примерно 1 из 10\n\n"
               "/theme кино — выбрать тему для этого чата.\n"
               "Разово: /q кино, /quiz_start 10 чгк")


async def cmd_theme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    c = store.chat(update.effective_chat.id)
    _, theme = split_theme(context.args or [])
    if not context.args:
        await msg.reply_text(f"Сейчас тема: {theme_name(c['theme'])}.\n\n" + THEME_USAGE)
        return
    if theme is None:
        await msg.reply_text("Не знаю такой темы.\n\n" + THEME_USAGE)
        return
    if not await is_admin(update, context):
        await msg.reply_text("Менять тему могут только администраторы чата.")
        return
    c["theme"] = theme
    store.save()
    await msg.reply_text(f"Тема чата: {theme_name(theme)}. Так будут выбираться вопросы для /q, "
                         "новых квизов и предложений сыграть.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat_id = update.effective_chat.id
    if not msg or not msg.text or not update.effective_user:
        return
    ensure_offer(context.application, chat_id)
    c = store.chat(chat_id)
    if not c["current"]:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        # в группе ответ принимается только реплаем на сообщение бота с вопросом
        reply = msg.reply_to_message
        ids = c["current"].get("message_ids")
        if not reply:
            return
        if ids is not None and reply.message_id not in ids:
            return
        if ids is None and not (reply.from_user and reply.from_user.id == context.bot.id):
            return  # вопрос задан старой версией бота — принимаем реплай на любое сообщение бота
    async with store.lock(chat_id):
        cur = c["current"]
        if not cur:
            return
        q = store.questions.get(cur["qid"])
        if not q:
            return
        user = update.effective_user
        ok = is_correct(msg.text, q["answer"], q.get("zachet", ""), q.get("text", ""))
        remember_attempt(c, msg.message_id, user, msg.text, cur["qid"], ok)
        if not ok:
            store.save()
            await mark_wrong(context.bot, chat_id, msg.message_id)
            return
        await msg.reply_text("✅ Верно!")
        await award(context.application, chat_id, user.id, user.full_name, cur["qid"], time.time(),
                    "🎉 Первым правильно ответил(а) <b>{name}</b> (+1, всего очков: {points})")


def remember_attempt(c: dict, message_id: int, user, text: str, qid: str, ok: bool) -> None:
    """Запоминаем ответы — на них потом можно подать апелляцию."""
    now = time.time()
    c["attempts"][str(message_id)] = {"uid": user.id, "name": user.full_name, "text": text,
                                      "qid": qid, "ts": now, "ok": ok}
    for k in [k for k, a in c["attempts"].items() if now - a["ts"] > APPEAL_WINDOW]:
        del c["attempts"][k]


async def award(app: Application, chat_id: int, uid: int, name: str, qid: str, ts: float,
                header: str) -> int:
    """+1 очко. Если вопрос ещё открыт — закрываем его (и в квизе идём к следующему).
    Вызывать под замком чата. Возвращает новое число очков."""
    c = store.chat(chat_id)
    s = c["scores"].setdefault(str(uid), {"name": name, "points": 0})
    s["name"] = name
    s["points"] += 1
    quiz = c["quiz"]
    if quiz and ts >= quiz.get("started", 0):
        qs = quiz["scores"].setdefault(str(uid), {"name": name, "points": 0})
        qs["points"] += 1
    key = f"{qid}:{uid}"
    if key not in c["credited"]:
        c["credited"] = (c["credited"] + [key])[-2000:]
    store.save()
    text = header.format(name=esc(name), points=s["points"])
    if c["current"] and c["current"]["qid"] == qid:
        for j in app.job_queue.get_jobs_by_name(f"timeout:{chat_id}"):
            j.schedule_removal()
        await reveal(app.bot, chat_id, text)
        if quiz:
            # сразу к следующему вопросу (или к итогам, если вопрос был последним)
            quiz["next_at"] = time.time() + NEXT_AFTER_CORRECT
            store.save()
            run_at(app, quiz_tick, chat_id, f"quiz:{chat_id}", quiz["next_at"])
    else:
        await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    return s["points"]


# ------------------------------------------------------------------ апелляции

def appeal_votes_needed() -> int:
    try:
        return max(1, int(os.getenv("APPEAL_VOTES", "3")))
    except ValueError:
        return 3


def appeal_text(c: dict, ap: dict) -> str:
    q = store.questions.get(ap["qid"], {})
    open_now = c["current"] and c["current"]["qid"] == ap["qid"]
    lines = ["⚖️ <b>Апелляция</b>",
             f"{esc(ap['by_name'])} просит засчитать ответ игрока <b>{esc(ap['name'])}</b>:",
             f"«{esc(ap['text'])}»", ""]
    if open_now:
        lines.append("Вопрос ещё открыт — правильный ответ покажу после решения.")
    elif q:
        lines.append(f"<b>Ответ:</b> {esc(q.get('answer', ''))}")
        if q.get("zachet"):
            lines.append(f"<b>Зачёт:</b> {esc(q['zachet'])}")
    lines += ["", f"Решает админ группы одним нажатием или {appeal_votes_needed()} голоса участников "
                  f"(сам игрок не голосует).",
              f"👍 За: {len(ap['yes'])}   👎 Против: {len(ap['no'])}"]
    return "\n".join(lines)


def appeal_keyboard(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Засчитать", callback_data=f"ap:{n}:y"),
                                  InlineKeyboardButton("❌ Отклонить", callback_data=f"ap:{n}:n")]])


async def cmd_appeal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat_id = update.effective_chat.id
    reply = msg.reply_to_message
    async with store.lock(chat_id):
        c = store.chat(chat_id)
        att = c["attempts"].get(str(reply.message_id)) if reply else None
        if not att:
            await msg.reply_text(
                "Чтобы подать апелляцию, ответьте командой /appeal (реплаем) на сообщение игрока "
                "с его ответом на вопрос. Можно на свой ответ или на чужой.")
            return
        if att["ok"] or f"{att['qid']}:{att['uid']}" in c["credited"]:
            await msg.reply_text(f"{att['name']} уже получил(а) очко за этот вопрос.")
            return
        if any(a["msg"] == reply.message_id and a["status"] == "open" for a in c["appeals"].values()):
            await msg.reply_text("Апелляция на этот ответ уже идёт — голосуйте под ней.")
            return
        c["appeal_seq"] += 1
        n = c["appeal_seq"]
        ap = {"msg": reply.message_id, "uid": att["uid"], "name": att["name"], "text": att["text"],
              "qid": att["qid"], "ts": att["ts"], "by": update.effective_user.id,
              "by_name": update.effective_user.full_name, "yes": [], "no": [], "status": "open"}
        c["appeals"][str(n)] = ap
        # старые апелляции не храним
        for k in [k for k, a in c["appeals"].items() if time.time() - a["ts"] > 2 * APPEAL_WINDOW]:
            del c["appeals"][k]
        store.save()
        sent = await context.bot.send_message(chat_id, appeal_text(c, ap), parse_mode=ParseMode.HTML,
                                              reply_markup=appeal_keyboard(n),
                                              reply_to_message_id=reply.message_id)
        ap["bot_msg"] = sent.message_id
        store.save()


async def on_appeal_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    chat = update.effective_chat
    try:
        _, n, vote = cq.data.split(":")
    except ValueError:
        await cq.answer()
        return
    voter = update.effective_user
    async with store.lock(chat.id):
        c = store.chat(chat.id)
        ap = c["appeals"].get(n)
        if not ap or ap["status"] != "open":
            await cq.answer("Эта апелляция уже решена.")
            return
        decider = chat.type == ChatType.PRIVATE
        if not decider:
            try:
                member = await context.bot.get_chat_member(chat.id, voter.id)
                decider = member.status in ("administrator", "creator")
            except TelegramError:
                decider = False
        if not decider and voter.id == ap["uid"]:
            await cq.answer("За свой ответ голосовать нельзя 🙂", show_alert=True)
            return
        if decider:
            result = vote
        else:
            mine, other = (ap["yes"], ap["no"]) if vote == "y" else (ap["no"], ap["yes"])
            if voter.id in other:
                other.remove(voter.id)
            if voter.id not in mine:
                mine.append(voter.id)
            need = appeal_votes_needed()
            result = "y" if len(ap["yes"]) >= need and len(ap["yes"]) > len(ap["no"]) else \
                     "n" if len(ap["no"]) >= need and len(ap["no"]) >= len(ap["yes"]) else None
        if result is None:
            store.save()
            await cq.answer("Голос учтён")
            try:
                await cq.edit_message_text(appeal_text(c, ap), parse_mode=ParseMode.HTML,
                                           reply_markup=appeal_keyboard(int(n)))
            except TelegramError:
                pass
            return
        ap["status"] = "accepted" if result == "y" else "rejected"
        who = f"админ {voter.full_name}" if decider and chat.type != ChatType.PRIVATE else "голосование"
        store.save()
        await cq.answer("Решено")
        if result == "y":
            already = f"{ap['qid']}:{ap['uid']}" in c["credited"]
            verdict = (f"✅ Апелляция принята ({esc(who)}), но {esc(ap['name'])} уже получил(а) очко "
                       f"за этот вопрос." if already else
                       f"✅ Апелляция принята ({esc(who)}): ответ «{esc(ap['text'])}» засчитан.")
            try:
                await cq.edit_message_text(verdict, parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
            if not already:
                await mark_right(context.bot, chat.id, ap["msg"])
                await award(context.application, chat.id, ap["uid"], ap["name"], ap["qid"], ap["ts"],
                            "⚖️ По апелляции очко получает <b>{name}</b> (+1, всего очков: {points})")
        else:
            try:
                await cq.edit_message_text(
                    f"❌ Апелляция отклонена ({esc(who)}): ответ «{esc(ap['text'])}» не засчитан.",
                    parse_mode=ParseMode.HTML)
            except TelegramError:
                pass


WRONG_REACTIONS = ["❌", "👎", "🤔", "🙈"]  # первая, которую разрешат Telegram и настройки группы
_wrong_ok: dict[int, str] = {}              # чат -> реакция, которая там сработала


async def mark_wrong(bot, chat_id: int, message_id: int) -> None:
    """Неверный ответ: реакция на сообщение вместо текста в чат."""
    tried = [_wrong_ok[chat_id]] if chat_id in _wrong_ok else WRONG_REACTIONS
    for emoji in tried:
        try:
            await bot.set_message_reaction(chat_id, message_id, emoji)
            _wrong_ok[chat_id] = emoji
            return
        except TelegramError as e:
            log.info("Реакция %s не поставлена в чате %s: %s", emoji, chat_id, e)
    _wrong_ok.pop(chat_id, None)  # в следующий раз снова попробуем все по порядку


async def mark_right(bot, chat_id: int, message_id: int) -> None:
    """После принятой апелляции меняем ❌ на 👍."""
    for emoji in ("👍", "🎉", "🔥"):
        try:
            await bot.set_message_reaction(chat_id, message_id, emoji)
            return
        except TelegramError:
            continue


async def cmd_daily_removed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Вопросов дня больше нет. Вместо них бот раз в день сам предлагает сыграть квиз — "
        "в то время, когда в чате обычно играют. Подробнее — /offer")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        log.info("Другая копия бота проверяет, работаю ли я (это нормально, она подождёт).")
        return
    log.error("Ошибка при обработке сообщения", exc_info=context.error)


# ------------------------------------------------------------------ одна копия бота

def wait_for_turn(token: str) -> None:
    """Если бот уже работает на другом компьютере, ждём, пока тот выключится.

    Telegram отдаёт сообщения только одному «слушателю». Пока другая копия слушает,
    наш запрос прерывается с ошибкой 409 (Conflict) — значит, нам надо подождать.
    Если ~30 секунд подряд никто не мешает — работаем сами. Сообщения при проверке
    не забираем (не подтверждаем), так что работающая копия их не теряет."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    told = False
    while True:
        quiet = 0.0
        busy = False
        while quiet < 30:
            t0 = time.monotonic()
            try:
                r = requests.post(url, json={"timeout": 20, "limit": 1}, timeout=40)
            except requests.RequestException as e:
                log.warning("Нет связи с Telegram (%s), пробую снова через 30 с", e)
                time.sleep(30)
                continue
            if r.status_code == 409:
                busy = True
                break
            if r.status_code == 401:
                print("Telegram не принял токен. Проверьте BOT_TOKEN в файле .env")
                sys.exit(1)
            took = time.monotonic() - t0
            quiet += took
            if took < 1:          # есть непрочитанные сообщения — даём другой копии их забрать
                time.sleep(3)
                quiet += 3
        if not busy:
            if told:
                log.info("Другая копия бота больше не отвечает — начинаю работать здесь.")
            return
        if not told:
            log.warning("Бот уже работает на другом компьютере. Эта копия ждёт и включится сама, "
                        "если та выключится. Окно можно не закрывать.")
            told = True
        time.sleep(random.uniform(90, 150))


# ------------------------------------------------------------------ запуск

async def restore_jobs(app: Application) -> None:
    """После перезапуска: вернуть квиз, вопрос дня и таймеры ответов."""
    now = time.time()
    for key, c in store.state["chats"].items():
        chat_id = int(key)
        c = store.chat(chat_id)
        if c["current"]:
            left = c["current"].get("timeout", ANSWER_TIMEOUT) - (now - c["current"]["asked_at"])
            schedule_timeout(app, chat_id, left)
        if c["quiz"]:
            run_at(app, quiz_tick, chat_id, f"quiz:{chat_id}", max(now + 5, c["quiz"]["next_at"]))
            log.info("Чат %s: квиз продолжается (%s из %s)", chat_id, c["quiz"]["asked"], c["quiz"]["total"])
        if c["offer"].get("enabled"):
            schedule_offer(app, chat_id)
            log.info("Чат %s: предложение сыграть — %s", chat_id, fmt_ts(c["offer"]["next_at"]))
    store.save()


# ------------------------------------------------------------------ общая память в Telegram
# У каждого компьютера (и каждой папки) свой state.json. Чтобы вопросы не повторялись и очки
# не расходились, работающий бот хранит копию state.json закреплённым файлом в личке владельца
# (OWNER_ID в .env), а бот, который начинает работу, сначала забирает её и объединяет со своей.

SYNC_EVERY = 90          # как часто (секунд) отправлять копию, если что-то изменилось
SYNC_CAPTION = "🗂 Память бота (не удаляйте и не открепляйте это сообщение)"


def owner_id() -> int | None:
    try:
        return int(os.getenv("OWNER_ID", "").strip())
    except ValueError:
        return None


def merge_states(a: dict, b: dict) -> dict:
    """Объединить две копии памяти: заданные вопросы — все из обеих, очки — большее,
    текущие квиз/вопрос/настройки — из более свежей копии."""
    new, old = (a, b) if a.get("saved_at", 0) >= b.get("saved_at", 0) else (b, a)
    out = json.loads(json.dumps(new))
    for cid, oc in old.get("chats", {}).items():
        nc = out["chats"].setdefault(cid, json.loads(json.dumps(oc)))
        if nc is oc or nc == oc:
            continue
        used = list(oc.get("used", []))
        seen = set(used)
        used += [q for q in nc.get("used", []) if q not in seen]
        nc["used"] = used
        scores = nc.setdefault("scores", {})
        for uid, sc in oc.get("scores", {}).items():
            if uid not in scores or sc.get("points", 0) > scores[uid].get("points", 0):
                scores[uid] = sc
        nc["credited"] = list(dict.fromkeys(oc.get("credited", []) + nc.get("credited", [])))[-2000:]
        nc["quiz_history"] = sorted(set(oc.get("quiz_history", []) + nc.get("quiz_history", [])))[-50:]
        att = dict(oc.get("attempts", {}))
        att.update(nc.get("attempts", {}))
        nc["attempts"] = att
    out["last_refresh"] = max(a.get("last_refresh", 0), b.get("last_refresh", 0))
    out["saved_at"] = max(a.get("saved_at", 0), b.get("saved_at", 0))
    return out


def merge_old_state_file() -> None:
    """state_old.json (память из старой папки бота) один раз добавляется к текущей памяти."""
    old = HERE / "state_old.json"
    if not old.exists():
        return
    merged = merge_states(load_json(STATE_FILE, {"chats": {}}), load_json(old, {"chats": {}}))
    save_json(STATE_FILE, merged)
    old.rename(HERE / f"state_old.merged-{int(time.time())}.json")
    log.info("Память из старой папки (state_old.json) добавлена.")


async def pull_shared_state(app: Application) -> None:
    """При запуске: забрать копию памяти из Telegram и объединить с локальной."""
    merge_old_state_file()
    oid = owner_id()
    if not oid:
        log.warning("OWNER_ID не задан — память между компьютерами не синхронизируется.")
        return
    try:
        chat = await app.bot.get_chat(oid)
        pin = chat.pinned_message
        if not pin or not pin.document or not (pin.caption or "").startswith(SYNC_CAPTION[:10]):
            log.info("Общей копии памяти в Telegram пока нет — создам её.")
            return
        f = await app.bot.get_file(pin.document.file_id)
        remote = json.loads(bytes(await f.download_as_bytearray()).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось забрать общую копию памяти из Telegram: %s", e)
        return
    local = load_json(STATE_FILE, {"chats": {}})
    merged = merge_states(local, remote)
    save_json(STATE_FILE, merged)
    log.info("Память объединена с копией из Telegram (вопросов задано: %d в %d чатах).",
             sum(len(c.get("used", [])) for c in merged["chats"].values()), len(merged["chats"]))


async def push_shared_state(app: Application, force: bool = False) -> None:
    """Отправить свежую копию памяти в личку владельца (заменить закреплённый файл)."""
    oid = owner_id()
    if not oid or not (store.dirty or force):
        return
    store.dirty = False
    data = json.dumps(store.state, ensure_ascii=False).encode("utf-8")
    caption = f"{SYNC_CAPTION}\nОбновлено: {fmt_ts(time.time())}"
    try:
        chat = await app.bot.get_chat(oid)
        pin = chat.pinned_message
        if pin and pin.document and (pin.caption or "").startswith(SYNC_CAPTION[:10]):
            await app.bot.edit_message_media(
                InputMediaDocument(data, filename="state.json", caption=caption),
                chat_id=oid, message_id=pin.message_id)
        else:
            m = await app.bot.send_document(oid, data, filename="state.json", caption=caption,
                                            disable_notification=True)
            await app.bot.pin_chat_message(oid, m.message_id, disable_notification=True)
    except TelegramError as e:
        store.dirty = True
        log.warning("Не удалось сохранить копию памяти в Telegram: %s", e)


async def sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await push_shared_state(context.application)


def apply_mark_used() -> None:
    """Одноразовая отметка из mark_used.json: старые вопросы ЧГК считаются уже заданными
    во всех чатах, где уже играли (кроме перечисленных в "except" и вопросов о кино).
    Каждый чат отмечается один раз — это запоминается в памяти (и в общей копии)."""
    task = load_json(HERE / "mark_used.json", None)
    if not task or not task.get("id"):
        return
    done = store.state.setdefault("migrations", {}).setdefault(task["id"], [])
    keep = set(task.get("except", []))
    old_ids = [qid for qid, q in store.questions.items() if q["category"] == "chgk" and qid not in keep]
    changed = 0
    for cid, c in store.state["chats"].items():
        if cid in done:
            continue
        done.append(cid)
        if not c.get("used") and not c.get("scores"):
            continue          # в этом чате ещё не играли — отмечать нечего
        used = set(c.setdefault("used", []))
        add = [qid for qid in old_ids if qid not in used]
        c["used"] += add
        changed += 1
        log.info("Чат %s: отмечено как уже заданные — %d старых вопросов ЧГК", cid, len(add))
    store.save()
    if changed:
        log.info("Отметка «%s» выполнена в %d чатах.", task["id"], changed)


async def post_shutdown(app: Application) -> None:
    await push_shared_state(app, force=True)


async def post_init(app: Application) -> None:
    global store
    merge_seed()
    await pull_shared_state(app)
    store = Store()
    apply_mark_used()
    store.dirty = True          # сразу отправим объединённую копию
    app.job_queue.run_repeating(sync_job, interval=SYNC_EVERY, first=15, name="sync")
    await app.bot.set_my_commands([
        ("q", "Вопрос прямо сейчас"),
        ("answer", "Показать ответ"),
        ("top", "Таблица лидеров"),
        ("quiz_start", "Квиз из N вопросов: /quiz_start 10"),
        ("quiz_stop", "Остановить квиз"),
        ("offer", "Предложения сыграть квиз: /offer on|off"),
        ("appeal", "Апелляция: реплаем на ответ игрока"),
        ("theme", "Тема вопросов: чгк, кино или все"),
        ("help", "Помощь"),
    ])
    await restore_jobs(app)
    schedule_refresh(app)
    me = await app.bot.get_me()
    log.info("Бот @%s запущен. Остановить — Ctrl+C", me.username)


SECRET_KEYS = ("BOT_TOKEN", "OWNER_ID")


def move_secrets_to_env() -> None:
    """Токен и Telegram ID владельца храним только в .env (он не попадает на GitHub и не
    заменяется при обновлении). Если они пока лежат в .env.default — переносим в .env."""
    default, env = HERE / ".env.default", HERE / ".env"
    if not default.exists():
        return
    from dotenv import dotenv_values
    have = dotenv_values(env) if env.exists() else {}
    src = dotenv_values(default)
    add = [f"{k}={src[k]}" for k in SECRET_KEYS
           if src.get(k) and "ВСТАВЬ" not in src[k] and not have.get(k)]
    if add:
        text = env.read_text(encoding="utf-8") if env.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        env.write_text(text + "\n".join(add) + "\n", encoding="utf-8")
        log.info("Токен/ID перенесены в .env (%s).", ", ".join(a.split("=")[0] for a in add))


def main() -> None:
    move_secrets_to_env()
    load_dotenv(HERE / ".env")            # ваши настройки (главнее)
    load_dotenv(HERE / ".env.default")    # настройки из архива — только то, чего нет в .env
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or "ВСТАВЬ" in token:
        print("Нет токена бота. Откройте файл .env и впишите строку BOT_TOKEN=ваш_токен")
        sys.exit(1)
    log.info("Проверяю, не запущен ли бот где-то ещё (до 30 секунд)…")
    wait_for_turn(token)
    global STARTED_AT
    STARTED_AT = time.time() - 35   # команды, отправленные во время проверки, выполняем
    app = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler(["help", "start"], fresh_only(cmd_help)))
    app.add_handler(CommandHandler("q", fresh_only(cmd_q)))
    app.add_handler(CommandHandler("answer", fresh_only(cmd_answer)))
    app.add_handler(CommandHandler("top", fresh_only(cmd_top)))
    app.add_handler(CommandHandler("quiz_start", fresh_only(cmd_quiz_start)))
    app.add_handler(CommandHandler("quiz_stop", fresh_only(cmd_quiz_stop)))
    app.add_handler(CommandHandler("offer", fresh_only(cmd_offer)))
    app.add_handler(CallbackQueryHandler(on_offer_button, pattern=r"^offer:"))
    app.add_handler(CommandHandler("daily", fresh_only(cmd_daily_removed)))
    app.add_handler(CommandHandler("theme", fresh_only(cmd_theme)))
    app.add_handler(CommandHandler("appeal", fresh_only(cmd_appeal)))
    app.add_handler(CallbackQueryHandler(on_appeal_button, pattern=r"^ap:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
