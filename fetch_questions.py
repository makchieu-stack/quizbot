"""
Скачивает вопросы «Что? Где? Когда?» с https://gotquestions.online/ в questions.json.

Как устроен сайт (проверено):
  * отдельного JSON-API для вопросов нет (/api/pack/<id>/ отвечает 404);
  * у каждого пакета есть официальная выгрузка /api/pack/<id>/download — файл Word (.docx)
    с точным текстом вопросов, ответов, зачётов, комментариев, источников и авторов;
  * ссылки на отдельные вопросы (/question/<id>) есть в HTML страницы пакета /pack/<id>;
  * страница /random требует входа в аккаунт, поэтому случайные пакеты выбираются по номеру.

Порядок работы для каждого пакета:
  1. Страница пакета /pack/<id>: название, номера вопросов. Если в странице встроены
     JSON-данные (Next.js: __NEXT_DATA__ или self.__next_f), берём вопросы прямо оттуда.
  2. Иначе — официальная выгрузка .docx (это тоже данные сайта, а не «парсинг вёрстки»).

Бережное отношение к сайту: пауза 2–3 с между запросами, не больше 20 пакетов за запуск,
уже просмотренные пакеты запоминаются в fetch_state.json и повторно не скачиваются.

Резервный источник — https://db.chgk.info/ (старая «База вопросов ЧГК»): если с
gotquestions.online ничего скачать не удалось, берутся случайные вопросы из XML-выдачи
db.chgk.info/xml/random/types1/limitN (types1 = вопросы «Что? Где? Когда?»).

Запуск:  python fetch_questions.py                 (5 пакетов, при неудаче — резерв)
         python fetch_questions.py --packs 20
         python fetch_questions.py --source chgk   (только db.chgk.info)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import zipfile
from html import unescape
from pathlib import Path

import requests

BASE = "https://gotquestions.online"
CHGK_BASE = "https://db.chgk.info"
CHGK_BATCH = 30            # вопросов за один запрос к db.chgk.info
CHGK_MAX_REQUESTS = 5      # запросов за запуск
HERE = Path(__file__).resolve().parent
QUESTIONS_FILE = HERE / "questions.json"
STATE_FILE = HERE / "fetch_state.json"
IMAGES_DIR = HERE / "images"
MAX_PACKS_PER_RUN = 20
DEFAULT_MAX_PACK_ID = 7346
USER_AGENT = "wingsquizbot-fetcher/1.0 (non-commercial Telegram quiz bot)"

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
# вопросы с аудио/видео бот показать не может — пропускаем
AV_PATTERN = re.compile(
    r"аудио|видео|прослушайте|послушайте|просмотрите ролик|\.mp3|\.mp4|youtu", re.I
)


# ---------------------------------------------------------------- сеть

class Polite:
    """HTTP-клиент с паузой 2–3 секунды перед каждым запросом."""

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.last = 0.0
        self.count = 0

    def get(self, url: str) -> requests.Response | None:
        wait = random.uniform(2.0, 3.0) - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.count += 1
        try:
            r = self.s.get(url, timeout=30)
        except requests.RequestException as e:
            log(f"  ошибка сети: {e}")
            return None
        if r.status_code == 429:
            log("  сайт просит притормозить (429) — прекращаю на этот раз")
            raise RateLimited()
        if r.status_code != 200:
            log(f"  {url} -> HTTP {r.status_code}")
            return None
        return r


class RateLimited(Exception):
    pass


class SkipMain(Exception):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- файлы

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- страница пакета

def parse_pack_page(html: str) -> dict:
    """Название пакета, номера вопросов по порядку и (если есть) встроенные JSON-вопросы."""
    title = ""
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if m:
        title = unescape(m.group(1)).strip()
        title = re.sub(r"\s*[|\-–—]\s*Got Questions.*$", "", title, flags=re.I).strip()
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if m:
        h1 = unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if h1:
            title = h1
    ids: list[str] = []
    for qid in re.findall(r'href="/question/(\d+)"', html):
        if qid not in ids:
            ids.append(qid)
    return {"title": title, "question_ids": ids, "embedded": find_embedded_questions(html)}


def _embedded_texts(html: str) -> list[str]:
    out = []
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        out.append(m.group(1))
    chunks = []
    for raw in re.findall(r"self\.__next_f\.push\((\[.*?\])\)</script>", html, re.S):
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if len(arr) > 1 and isinstance(arr[1], str):
            chunks.append(arr[1])
    if chunks:
        out.append("".join(chunks))
    return out


def find_embedded_questions(html: str) -> list[dict]:
    """Ищет во встроенных данных страницы объекты, похожие на вопросы."""
    found: dict[str, dict] = {}

    def walk(obj):
        if isinstance(obj, dict):
            if "answer" in obj and ("text" in obj or "question" in obj):
                q = normalize_embedded(obj)
                if q:
                    found.setdefault(q["id"] or str(len(found)), q)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    dec = json.JSONDecoder()
    for text in _embedded_texts(html):
        try:
            walk(json.loads(text))
            continue
        except json.JSONDecodeError:
            pass
        # RSC-поток: JSON-фрагменты вперемешку со служебными строками
        for m in re.finditer(r'[\[{]', text):
            if '"answer"' not in text[m.start(): m.start() + 20000]:
                continue
            try:
                obj, _ = dec.raw_decode(text, m.start())
            except json.JSONDecodeError:
                continue
            walk(obj)
    return list(found.values())


def _pick(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return ""


def normalize_embedded(d: dict) -> dict | None:
    text = _pick(d, "text", "question")
    answer = _pick(d, "answer")
    if not isinstance(text, str) or not isinstance(answer, str):
        return None
    authors = _pick(d, "authors", "author")
    if isinstance(authors, list):
        authors = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in authors)
    source = _pick(d, "source", "sources")
    if isinstance(source, list):
        source = "\n".join(map(str, source))
    pic = _pick(d, "razdatkaPic", "razdatka_pic", "handoutPic", "image")
    return {
        "id": str(_pick(d, "id") or ""),
        "text": text,
        "razdatka": str(_pick(d, "razdatkaText", "razdatka_text") or ""),
        "answer": answer,
        "zachet": str(_pick(d, "zachet", "accepted") or ""),
        "comment": str(_pick(d, "comment") or ""),
        "source": str(source or ""),
        "author": str(authors or ""),
        "image_url": pic if isinstance(pic, str) else "",
    }


# ---------------------------------------------------------------- выгрузка .docx

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
LABELS = [
    ("answer", re.compile(r"^Ответ\s*:\s*", re.I)),
    ("zachet", re.compile(r"^Зач[её]т[^:\n]{0,40}:\s*", re.I)),
    ("nezachet", re.compile(r"^Незач[её]т[^:\n]{0,40}:\s*", re.I)),
    ("comment", re.compile(r"^Комментари[йи]\s*:\s*", re.I)),
    ("source", re.compile(r"^Источники?\s*:\s*", re.I)),
    ("author", re.compile(r"^Автор(ы)?\s*:\s*", re.I)),
]
Q_HEAD = re.compile(r"^Вопрос\s+(\d+)\.?\s*$")
STOP = re.compile(r"^(Тур\s+\d+|Редактор(ы)?\s*:|Блок\s+\d+|Перестрелка|Дополнительн)", re.I)


def _docx_paragraphs(data: bytes):
    """[(текст абзаца, [пути картинок внутри архива], [гиперссылки])]"""
    import xml.etree.ElementTree as ET

    z = zipfile.ZipFile(io.BytesIO(data))
    rels = {}
    try:
        rroot = ET.fromstring(z.read("word/_rels/document.xml.rels"))
        for r in rroot:
            rels[r.get("Id")] = r.get("Target", "")
    except KeyError:
        pass
    root = ET.fromstring(z.read("word/document.xml"))
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    out = []
    for p in root.iter(W + "p"):
        parts = []
        for el in p.iter():
            if el.tag == W + "t" and el.text:
                parts.append(el.text)
            elif el.tag == W + "tab":
                parts.append("\t")
            elif el.tag == W + "br":
                parts.append("\n")
        imgs, links = [], []
        for el in p.iter():
            emb = el.get(R + "embed") or (el.get(R + "id") if el.tag.endswith("imagedata") else None)
            if emb and emb in rels:
                imgs.append("word/" + rels[emb].lstrip("/").replace("../", ""))
            if el.tag == W + "hyperlink" and el.get(R + "id") in rels:
                links.append(rels[el.get(R + "id")])
        out.append(("".join(parts).strip(), imgs, links))
    return z, out


def parse_docx(data: bytes) -> tuple[str, list[dict]]:
    """Возвращает (название пакета, список вопросов в порядке следования)."""
    z, paras = _docx_paragraphs(data)
    title = next((t for t, _, _ in paras if t), "")
    questions: list[dict] = []
    cur: dict | None = None
    field = None
    for text, imgs, links in paras:
        mh = Q_HEAD.match(text)
        if mh:
            cur = {"number": int(mh.group(1)), "text": [], "answer": [], "zachet": [],
                   "nezachet": [], "comment": [], "source": [], "author": [],
                   "images": [], "links": []}
            questions.append(cur)
            field = "text"
            continue
        if cur is None:
            continue
        if STOP.match(text):
            cur = None
            field = None
            continue
        for name, rx in LABELS:
            if rx.match(text):
                field = name
                text = rx.sub("", text, count=1)
                break
        if field == "text":
            cur["images"] += imgs
            cur["links"] += links
        if text and field:
            cur[field].append(text)
    result = []
    for q in questions:
        item = {k: "\n".join(v).strip() for k, v in q.items()
                if k not in ("number", "images", "links")}
        item["number"] = q["number"]
        item["image_bytes"] = None
        item["image_url"] = ""
        if q["images"]:
            try:
                item["image_bytes"] = (z.read(q["images"][0]), Path(q["images"][0]).suffix or ".png")
            except KeyError:
                pass
        for link in q["links"]:
            if link.lower().split("?")[0].endswith(IMAGE_EXT):
                item["image_url"] = link
                break
        result.append(item)
    return title, result


# ---------------------------------------------------------------- сборка

def has_unsupported_media(q: dict) -> bool:
    return bool(AV_PATTERN.search(q.get("text", "") + " " + q.get("razdatka", "")))


def has_text_handout(text: str) -> bool:
    """Раздатка дана текстом прямо в вопросе («Раздаточный материал: …»), а не картинкой."""
    return bool(re.search(r"раздаточн\w* материал\w*\s*:\s*\S", text, re.I)) and \
        not re.search(r"раздаточн\w* материал\w*\s*\((изображ|картин|фото|рисун)", text, re.I)


def mentions_handout(q: dict) -> bool:
    """Вопрос ссылается на картинку/раздатку: «раздаточный материал», «перед вами картинка»…"""
    return bool(re.search(r"раздат|перед вами[^.?!]{0,40}(картин|изображ|фото|рисун|плакат|постер|схем|кадр)|"
                          r"на (этой |данной )?(картинке|фотографии|изображении|рисунке)", q.get("text", ""), re.I))


def save_image(content: bytes, qid: str, ext: str) -> str:
    IMAGES_DIR.mkdir(exist_ok=True)
    ext = ext if ext.lower() in IMAGE_EXT else ".png"
    path = IMAGES_DIR / f"{qid}{ext.lower()}"
    path.write_bytes(content)
    return f"images/{path.name}"


def fetch_pack(http: Polite, pack_id: int) -> list[dict] | None:
    """None — пакета нет; [] — пакет есть, но подходящих вопросов нет."""
    r = http.get(f"{BASE}/pack/{pack_id}")
    if r is None:
        return None
    page = parse_pack_page(r.text)
    title = page["title"]
    ids = page["question_ids"]

    if page["embedded"]:
        log(f"  данные взяты из JSON, встроенного в страницу ({len(page['embedded'])} вопр.)")
        raw = page["embedded"]
        for i, q in enumerate(raw):
            if not q["id"] and i < len(ids):
                q["id"] = ids[i]
    else:
        d = http.get(f"{BASE}/api/pack/{pack_id}/download")
        if d is None:
            return []
        try:
            doc_title, raw = parse_docx(d.content)
        except (zipfile.BadZipFile, KeyError, Exception) as e:  # noqa: BLE001
            log(f"  не удалось разобрать выгрузку: {e}")
            return []
        title = title or doc_title
        if len(raw) == len(ids):
            for q, qid in zip(raw, ids):
                q["id"] = qid
        else:
            log(f"  число вопросов в выгрузке ({len(raw)}) и на странице ({len(ids)}) "
                f"не совпало — ссылки будут вести на пакет")
            for q in raw:
                q["id"] = ""

    out = []
    for i, q in enumerate(raw, 1):
        if not q.get("text") or not q.get("answer"):
            continue
        if has_unsupported_media(q):
            continue
        qid = q["id"] or f"p{pack_id}-{q.get('number', i)}"
        image = None
        if q.get("image_bytes"):
            image = save_image(q["image_bytes"][0], qid, q["image_bytes"][1])
        elif q.get("image_url"):
            url = q["image_url"]
            if url.startswith("/"):
                url = BASE + url
            img = http.get(url)
            if img is not None and img.headers.get("content-type", "").startswith("image/"):
                image = save_image(img.content, qid, Path(url.split("?")[0]).suffix)
        if image is None and mentions_handout(q) and not q.get("razdatka") \
                and not has_text_handout(q["text"]):
            # раздатка-картинка, которую не удалось получить — такой вопрос не задать
            continue
        out.append({
            "id": qid,
            "text": q["text"],
            "razdatka": q.get("razdatka", ""),
            "answer": q["answer"],
            "zachet": q.get("zachet", ""),
            "comment": q.get("comment", ""),
            "source": q.get("source", ""),
            "author": q.get("author", ""),
            "pack_title": title,
            "pack_id": pack_id,
            "url": f"{BASE}/question/{q['id']}" if q["id"] else f"{BASE}/pack/{pack_id}",
            "image": image,
        })
    return out


# ---------------------------------------------------------------- резерв: db.chgk.info

CHGK_PIC = re.compile(r"\(pic:\s*([^)\s]+)\s*\)", re.I)
CHGK_AV = re.compile(r"\((aud|video|vid):", re.I)


def chgk_text(s: str) -> str:
    """Убираем только переносы строк вёрстки базы: абзац там начинается с отступа."""
    s = (s or "").replace("\r", "").strip("\n")
    s = re.sub(r"\n[ \t]{2,}", "\n\n", s)        # новый абзац
    s = re.sub(r"(?<!\n)\n(?!\n)", " ", s)          # мягкий перенос внутри абзаца
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def parse_chgk_xml(data: bytes) -> list[dict]:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        if el.tag.lower() != "question" or len(el) == 0:
            continue
        f = {child.tag.lower(): (child.text or "") for child in el}
        out.append(f)
    return out


def fetch_chgk(http: Polite, known_ids: set[str], requests_left: int) -> list[dict]:
    result = []
    for _ in range(requests_left):
        r = http.get(f"{CHGK_BASE}/xml/random/types1/limit{CHGK_BATCH}")
        if r is None:
            break
        try:
            raw = parse_chgk_xml(r.content)
        except Exception as e:  # noqa: BLE001
            log(f"  db.chgk.info: не удалось разобрать ответ: {e}")
            break
        before = len(result)
        for f in raw:
            text, answer = chgk_text(f.get("question", "")), chgk_text(f.get("answer", ""))
            if not text or not answer:
                continue
            tour_file, number = f.get("tourfilename", "").strip(), f.get("number", "").strip()
            qid = "chgk:" + (f"{tour_file}-{number}" if tour_file and number
                             else f.get("questionid", "").strip() or hashlib.md5(text.encode()).hexdigest()[:12])
            if qid in known_ids:
                continue
            comment = chgk_text(f.get("comments", ""))
            if CHGK_AV.search(text) or CHGK_PIC.search(answer + comment):
                continue
            image = None
            pic = CHGK_PIC.search(text)
            if pic:
                img = http.get(f"{CHGK_BASE}/images/db/{pic.group(1)}")
                if img is None or not img.headers.get("content-type", "").startswith("image/"):
                    continue
                image = save_image(img.content, qid.replace(":", "_"), Path(pic.group(1)).suffix)
                text = CHGK_PIC.sub("", text).strip()
            title = ", ".join(x for x in (f.get("tournamenttitle", "").strip(),
                                          f.get("tourtitle", "").strip()) if x)
            url = (f"{CHGK_BASE}/question/{tour_file}/{number}" if tour_file and number
                   else f"{CHGK_BASE}/tour/{tour_file}" if tour_file else CHGK_BASE)
            known_ids.add(qid)
            result.append({
                "id": qid, "text": text, "razdatka": "", "answer": answer,
                "zachet": chgk_text(f.get("passcriteria", "")), "comment": comment,
                "source": chgk_text(f.get("sources", "")), "author": chgk_text(f.get("authors", "")),
                "pack_title": title, "pack_id": None, "url": url, "image": image,
                "site": "db.chgk.info",
            })
        log(f"  db.chgk.info: новых вопросов {len(result) - before}")
    return result


def text_key(q: dict) -> str:
    """Ключ для поиска одинаковых вопросов из разных баз: только буквы и цифры."""
    return re.sub(r"[^0-9a-zа-я]", "", q.get("text", "").lower().replace("ё", "е"))[:300]


def find_max_pack_id(http: Polite) -> int:
    r = http.get(f"{BASE}/search?type=packages")
    if r is not None:
        nums = [int(n) for n in re.findall(r'href="/pack/(\d+)"', r.text)]
        if nums:
            return max(nums)
    return DEFAULT_MAX_PACK_ID


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packs", type=int, default=5, help="сколько новых пакетов скачать (макс. 20)")
    ap.add_argument("--pack", type=int, action="append", help="скачать конкретный пакет по номеру")
    ap.add_argument("--source", choices=["auto", "gotquestions", "chgk"], default="auto",
                    help="откуда качать: auto — gotquestions.online, при неудаче db.chgk.info")
    args = ap.parse_args()
    want = max(1, min(args.packs, MAX_PACKS_PER_RUN))

    db = load_json(QUESTIONS_FILE, {"questions": []})
    state = load_json(STATE_FILE, {"seen_packs": [], "max_pack_id": 0})
    seen = set(state.get("seen_packs", []))
    known_ids = {q["id"] for q in db["questions"]}
    known_texts = {text_key(q) for q in db["questions"]}
    seen |= {q["pack_id"] for q in db["questions"] if q.get("pack_id")}

    http = Polite()
    added = 0
    tried = 0
    try:
        if args.source == "chgk":
            raise SkipMain()
        if args.pack:
            candidates = [p for p in args.pack if p not in seen]
        else:
            max_id = find_max_pack_id(http)
            state["max_pack_id"] = max_id
            pool = [p for p in range(1, max_id + 1) if p not in seen]
            random.shuffle(pool)
            candidates = pool
        got = 0
        for pack_id in candidates:
            if got >= want or tried >= MAX_PACKS_PER_RUN:
                break
            tried += 1
            log(f"Пакет {pack_id}...")
            qs = fetch_pack(http, pack_id)
            seen.add(pack_id)
            if qs is None:
                continue
            got += 1
            new = []
            for q in qs:
                if q["id"] not in known_ids and text_key(q) not in known_texts:
                    new.append(q)
                    known_texts.add(text_key(q))
            db["questions"].extend(new)
            known_ids |= {q["id"] for q in new}
            added += len(new)
            log(f"  «{new[0]['pack_title'] if new else ''}»: добавлено вопросов: {len(new)}")
            # сохраняем после каждого пакета, чтобы ничего не потерять
            state["seen_packs"] = sorted(seen)
            save_json(QUESTIONS_FILE, db)
            save_json(STATE_FILE, state)
    except (RateLimited, SkipMain):
        pass
    except KeyboardInterrupt:
        log("Прервано.")
    try:
        if added == 0 and args.source in ("auto", "chgk"):
            log("Пробую резервную базу db.chgk.info...")
            extra = []
            for q in fetch_chgk(http, known_ids, CHGK_MAX_REQUESTS):
                if text_key(q) not in known_texts:
                    extra.append(q)
                    known_texts.add(text_key(q))
            db["questions"].extend(extra)
            added += len(extra)
    except (RateLimited, KeyboardInterrupt):
        pass
    finally:
        state["seen_packs"] = sorted(seen)
        save_json(QUESTIONS_FILE, db)
        save_json(STATE_FILE, state)
    log(f"Готово: просмотрено пакетов {tried}, новых вопросов {added}, "
        f"всего в базе {len(db['questions'])}.")
    return 0 if added else 1


if __name__ == "__main__":
    sys.exit(main())
