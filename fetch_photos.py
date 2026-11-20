"""
Скачивает фотографии актёров для темы «Угадай фильм по актёрам» с Викисклада
(Wikimedia Commons) — только свободные фото, с автором и лицензией.

Как ищем:
  1. Фильм находим в Викиданных по названию и году, берём его актёрский состав (P161).
  2. Наших трёх актёров ищем в этом составе по имени и берём их фото (P18).
  3. Если фильм не нашёлся или актёра нет в составе — ищем актёра прямо по имени
     и проверяем, что это актёр/актриса.
  4. С Викисклада берём уменьшенную копию фото (до 800 px), автора и лицензию.

Результат: папка images/actors/ и файл actors_photos.json. Бот задаёт вопрос о фильме
только если есть фото всех трёх актёров. Повторный запуск докачивает недостающее,
уже найденное не запрашивает снова.

Запуск:  python fetch_photos.py        (или двойной щелчок по fetch_photos.bat)
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
MOVIES = HERE / "custom_movies.json"
OUT = HERE / "actors_photos.json"
CACHE = HERE / "photos_cache.json"
IMG_DIR = HERE / "images" / "actors"
WD = "https://www.wikidata.org/w/api.php"
COMMONS = "https://commons.wikimedia.org/w/api.php"
HEADERS = {"User-Agent": "wingsquizbot/1.0 (non-commercial Telegram quiz bot; https://t.me/wingsquizbot)"}
PAUSE = 1.0          # секунда между запросами — Викимедиа просит не спешить
THUMB_WIDTH = 800

FILM_CLASSES = {"Q11424", "Q506240", "Q5398426", "Q1259759", "Q202866", "Q24862", "Q229390",
                "Q17517379", "Q21191270", "Q24856", "Q130232", "Q157394", "Q2484376", "Q188473"}
ACTOR_WORDS = ("актёр", "актер", "актриса", "actor", "actress")
ALLOWED_LICENSES = re.compile(r"^(CC0|CC BY|CC-BY|Public domain|PD|Attribution|GFDL|FAL)", re.I)

session = requests.Session()
session.headers.update(HEADERS)
_last = 0.0


def log(msg: str) -> None:
    print(msg, flush=True)


def get(url: str, params: dict | None = None, raw: bool = False):
    global _last
    for attempt in range(4):
        wait = PAUSE - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
        try:
            r = session.get(url, params=params, timeout=40)
        except requests.RequestException as e:
            log(f"   сеть: {e}; пробую ещё раз")
            time.sleep(10)
            continue
        if r.status_code == 429:
            pause = int(r.headers.get("Retry-After", "30") or 30)
            log(f"   сервер просит подождать {pause} с…")
            time.sleep(min(pause, 120))
            continue
        if r.status_code != 200:
            log(f"   HTTP {r.status_code} для {url}")
            return None
        return r.content if raw else r.json()
    return None


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"\(.*?\)|«.*?»", " ", s)
    s = re.sub(r"-?младший|-?старший|jr\.?|sr\.?", " ", s)
    s = re.sub(r"[^a-zа-я0-9 ]", " ", s)
    return " ".join(s.split())


def same_person(our: str, label: str) -> bool:
    a, b = norm(our).split(), norm(label).split()
    if not a or not b:
        return False
    if "".join(a) == "".join(b):          # «ДиКаприо» ~ «Ди Каприо»
        return True
    if a[0][0] != b[0][0]:
        return False
    # фамилия совпадает, имя — по первой букве (Сэмюэл Л. Джексон ~ Сэмюэл Джексон)
    return a[-1] == b[-1] or "".join(a[1:]) == "".join(b[1:])


def entities(ids: list[str], props: str = "claims|labels|descriptions") -> dict:
    out = {}
    for i in range(0, len(ids), 50):
        d = get(WD, {"action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "props": props,
                     "languages": "ru|en", "format": "json"})
        out.update((d or {}).get("entities", {}))
    return out


def claim_ids(ent: dict, prop: str) -> list[str]:
    res = []
    for c in ent.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and "id" in v:
            res.append(v["id"])
        elif isinstance(v, str):
            res.append(v)
    return res


def years(ent: dict) -> set[int]:
    ys = set()
    for c in ent.get("claims", {}).get("P577", []):
        t = c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time", "")
        m = re.match(r"[+-](\d{4})", t)
        if m:
            ys.add(int(m.group(1)))
    return ys


def label(ent: dict) -> list[str]:
    return [v["value"] for v in ent.get("labels", {}).values()]


def find_film(q: dict, cache: dict) -> str | None:
    key = f"film:{q['id']}"
    if key in cache:
        return cache[key]
    found = None
    for title, lang in ((q["answer"], "ru"), (q.get("en_title", ""), "en")):
        if not title or found:
            continue
        d = get(WD, {"action": "wbsearchentities", "search": title, "language": lang, "uselang": lang,
                     "type": "item", "limit": 8, "format": "json"})
        ids = [x["id"] for x in (d or {}).get("search", [])]
        if not ids:
            continue
        ents = entities(ids, "claims")
        for i in ids:
            e = ents.get(i, {})
            if not FILM_CLASSES & set(claim_ids(e, "P31")):
                continue
            ys = years(e)
            if not ys or any(abs(y - q["year"]) <= 1 for y in ys):
                found = i
                break
    cache[key] = found
    return found


def find_actor_direct(name: str, cache: dict) -> str | None:
    key = f"actor:{name}"
    if key in cache:
        return cache[key]
    found = None
    clean = re.sub(r"\(.*?\)", "", name).strip()
    d = get(WD, {"action": "wbsearchentities", "search": clean, "language": "ru", "uselang": "ru",
                 "type": "item", "limit": 5, "format": "json"})
    for x in (d or {}).get("search", []):
        desc = (x.get("description") or "").lower()
        if any(w in desc for w in ACTOR_WORDS) and same_person(clean, x.get("label", "")):
            found = x["id"]
            break
    cache[key] = found
    return found


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return " ".join(html.unescape(s).split())


def commons_info(files: list[str]) -> dict:
    """Имя файла -> {thumb, page, author, license}."""
    out = {}
    for i in range(0, len(files), 40):
        chunk = files[i:i + 40]
        d = get(COMMONS, {"action": "query", "titles": "|".join("File:" + f for f in chunk),
                          "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": THUMB_WIDTH,
                          "format": "json"})
        if not d:
            continue
        norm_map = {n["to"]: n["from"] for n in d.get("query", {}).get("normalized", [])}
        for page in d.get("query", {}).get("pages", {}).values():
            ii = (page.get("imageinfo") or [{}])[0]
            meta = ii.get("extmetadata", {})
            title = norm_map.get(page.get("title", ""), page.get("title", ""))
            out[title.replace("File:", "", 1).replace("_", " ")] = {
                "thumb": ii.get("thumburl") or ii.get("url"),
                "page": ii.get("descriptionurl", ""),
                "author": strip_html(meta.get("Artist", {}).get("value", "")) or "автор не указан",
                "license": strip_html(meta.get("LicenseShortName", {}).get("value", "")),
            }
    return out


def main() -> int:
    movies = json.loads(MOVIES.read_text(encoding="utf-8"))["questions"]
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    result = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Фильмов: {len(movies)}. Ищу фото актёров — это займёт 15–20 минут, окно не закрывайте.\n")

    actor_file: dict[tuple[str, str], str | None] = {}   # (фильм, актёр) -> файл на Викискладе
    bad_license: set[str] = set()
    try:
        for n, q in enumerate(movies, 1):
            if q["id"] in result and [p["actor"] for p in result[q["id"]]] == q["actors"]:
                continue
            result.pop(q["id"], None)   # состав актёров в вопросе изменился — ищем заново
            log(f"[{n}/{len(movies)}] {q['answer']}")
            film = find_film(q, cache)
            cast = {}
            if film:
                ents = entities([film], "claims")
                cast_ids = claim_ids(ents.get(film, {}), "P161")
                if cast_ids:
                    cast = entities(cast_ids, "claims|labels")
            for actor in q["actors"]:
                key = f"photo:{q['id']}:{actor}"
                if key in cache:
                    actor_file[(q["id"], actor)] = cache[key]
                    continue
                aid = next((i for i, e in cast.items() if any(same_person(actor, l) for l in label(e))), None)
                if aid is None:
                    aid = find_actor_direct(actor, cache)
                    if aid:
                        cast.update(entities([aid], "claims|labels"))
                pic = claim_ids(cast.get(aid, {}), "P18")[:1] if aid else []
                actor_file[(q["id"], actor)] = cache[key] = pic[0] if pic else None
                log(f"     {actor}: {'есть фото' if pic else 'фото нет'}")
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except KeyboardInterrupt:
        log("Прервано — найденное сохранено, при следующем запуске продолжу.")

    # описания файлов и скачивание
    need = sorted({f for f in actor_file.values() if f})
    info = commons_info(need) if need else {}
    for q in movies:
        files = [actor_file.get((q["id"], a)) for a in q["actors"]]
        if (q["id"] in result and [p["actor"] for p in result[q["id"]]] == q["actors"]) or not all(files):
            continue
        photos = []
        for actor, f in zip(q["actors"], files):
            meta = info.get(f.replace("_", " "))
            if not meta or not meta["thumb"] or not ALLOWED_LICENSES.search(meta["license"] or ""):
                bad_license.add(actor)
                photos = None
                break
            ext = Path(meta["thumb"].split("?")[0]).suffix.lower() or ".jpg"
            name = hashlib.md5(f.encode()).hexdigest()[:16] + (ext if ext in (".jpg", ".jpeg", ".png") else ".jpg")
            path = IMG_DIR / name
            if not path.exists():
                data = get(meta["thumb"], raw=True)
                if not data:
                    photos = None
                    break
                path.write_bytes(data)
            photos.append({"actor": actor, "file": f"images/actors/{name}", "author": meta["author"],
                           "license": meta["license"], "page": meta["page"]})
        if photos:
            result[q["id"]] = photos
            OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    full = sum(1 for q in movies if q["id"] in result)
    log(f"\nГОТОВО: фильмов с фото всех трёх актёров — {full} из {len(movies)}.")
    def why(q):
        parts = [a for a in q["actors"] if not actor_file.get((q["id"], a))]
        parts += [f"{a} (фото без свободной лицензии)" for a in q["actors"]
                  if actor_file.get((q["id"], a)) and a in bad_license]
        return ", ".join(parts) or "не удалось скачать фото"
    missing = [f"{q['answer']}: {why(q)}" for q in movies if q["id"] not in result]
    if missing:
        log("Без фото (эти вопросы бот задавать не будет):")
        for m in missing:
            log("  • " + m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
