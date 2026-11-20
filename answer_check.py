"""Мягкая проверка ответов: регистр, ё/е, кавычки, пунктуация, 1–2 опечатки в длинных словах."""
from __future__ import annotations

import re
from itertools import permutations

# слова, которые можно не писать или дописать лишние
STOP_WORDS = {"и", "в", "во", "на", "с", "со", "из", "от", "для", "к", "ко", "по", "о", "об",
              "у", "за", "the", "a", "an", "of"}
FILLERS = {"это", "ответ", "наверное", "наверно", "может", "быть", "думаю", "я", "мне",
           "кажется", "мб", "ну", "вероятно", "конечно", "версия", "наш", "моя", "мой",
           "буква", "буквы"}
# служебные пометки в «Зачёте», которые не являются вариантами ответа
META = re.compile(
    r"точный ответ|по смыслу|в любом порядке|в любой форме|в любом падеже|по фамилии|"
    r"и т\.\s*п\.?|и т\.\s*д\.?|и тому подобное|с упоминанием|любые|аналогичные|"
    r"синонимичные|без уточнений|по ключевому слову|ответы|ответ",
    re.I,
)


def normalize(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)  # кавычки, пунктуация, дефисы
    s = s.replace("_", " ")
    return " ".join(s.split())


def _tokens(s: str, drop_fillers: bool = False) -> list[str]:
    all_words = normalize(s).split()
    words = [w for w in all_words if w not in STOP_WORDS]
    if drop_fillers:
        words = [w for w in words if w not in FILLERS]
    # если ответ целиком из «служебных» слов (например, буква «В»), ничего не выбрасываем
    if words:
        return words
    return [w for w in all_words if w not in FILLERS] or all_words if drop_fillers else all_words


def levenshtein(a: str, b: str, limit: int = 3) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


try:  # основа слова: «оранжевая», «оранжевого» → «оранжев»
    import snowballstemmer
    _STEM = snowballstemmer.stemmer("russian")
except ImportError:  # библиотека не установлена — работаем без неё
    _STEM = None


def same_stem(user: str, right: str) -> bool:
    if _STEM is None:
        return False
    su, sr = _STEM.stemWord(user), _STEM.stemWord(right)
    if su != sr:
        return False
    if len(sr) >= 4:
        return True
    # короткая основа («син», «кот»): только если оба слова длинные (синий/синего)
    # или одно из слов и есть основа (кот/кота); «пяти»/«пятый» не совпадут
    return min(len(user), len(right)) >= 5 or sr in (user, right)


def word_ok(user: str, right: str) -> bool:
    if user == right:
        return True
    if user.isdigit() or right.isdigit():
        return False
    n = len(right)
    allowed = 2 if n >= 8 else 1 if n >= 5 else 0
    if allowed and levenshtein(user, right, allowed) <= allowed:
        return True
    if same_stem(user, right):
        return True
    # другие окончания: «пираньи» ~ «пираний», «трезубец» ~ «трезубца»
    if min(len(user), n) >= 5 and abs(len(user) - n) <= 2:
        k = 0
        while k < min(len(user), n) and user[k] == right[k]:
            k += 1
        if k >= max(4, n - 2):
            return True
    return False


def _words_match(user: list[str], right: list[str], any_order: bool) -> bool:
    if len(user) != len(right) or not right:
        return False
    orders = permutations(user) if any_order and len(user) <= 6 else [user]
    return any(all(word_ok(u, r) for u, r in zip(o, right)) for o in orders)


def _strip_brackets(s: str) -> list[str]:
    """[необязательная часть] и (пояснения) — варианты с ними и без них."""
    out = {s}
    out.add(re.sub(r"\[[^\]]*\]", " ", s))
    out.add(re.sub(r"[\[\]]", " ", s))
    out.add(re.sub(r"\([^)]*\)", " ", s))
    out.add(re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s))
    return [x for x in out if x.strip()]


INLINE_LABEL = re.compile(r"(Незач[её]т|Зач[её]т|Комментари[йи]|Источники?|Автор\w*)\b[^:\n]{0,40}:", re.I)


def split_inline(answer: str, zachet: str) -> tuple[str, str]:
    """«Зубного. Зачет (так и быть): стоматолога» — зачёт, записанный прямо в строке ответа."""
    parts = INLINE_LABEL.split(answer)
    if len(parts) == 1:
        return answer, zachet
    main, extra = parts[0], []
    for label, body in zip(parts[1::2], parts[2::2]):
        if re.match(r"зач", label, re.I):
            extra.append(body)
    return main, "; ".join([z for z in [zachet] + extra if z.strip()])


def candidates(answer: str, zachet: str) -> tuple[list[str], dict]:
    answer, zachet = split_inline(answer, zachet)
    flags = {
        "any_order": bool(re.search(r"любом порядке", zachet, re.I)),
        "surname": bool(re.search(r"по фамилии", zachet, re.I)),
        "meaning": bool(re.search(r"по смыслу", zachet, re.I)),
    }
    first_line = answer.strip().split("\n")[0]  # иногда после ответа идёт пояснение
    raw = [answer, first_line] + re.split(r"\s+или\s+", first_line)
    # если сам ответ из нескольких частей через запятую, «Зачёт» по запятым не дробим
    split_rx = r"\s+или\s+" if "," in answer else r"\s+или\s+|,\s*"
    for part in re.split(r"[;\n]", zachet):
        if re.search(r"упоминани|обязательн", part, re.I):
            continue  # «с упоминанием X и Y» — это условие, а не список вариантов
        part = META.sub(" ", part)
        raw.append(part)
        raw += re.split(split_rx, part)
        raw += re.findall(r"[«\"“]([^»\"”]+)[»\"”]", part)
    out: list[str] = []
    for r in raw:
        for v in _strip_brackets(r):
            if _tokens(v) and v not in out:
                out.append(v)
    if flags["surname"]:
        words = _tokens(answer)
        if len(words) >= 2:
            out.append(words[-1])
    return out, flags


def is_correct(user_text: str, answer: str, zachet: str = "", question: str = "") -> bool:
    user = _tokens(user_text, drop_fillers=True)
    if not user or len(user) > 12:
        return False
    if _is_correct(user, answer, zachet):
        return True
    # «моя любовь» при ответе «Моя любовь»: пустышки бывают частью самого ответа
    user_raw = _tokens(user_text)
    if user_raw != user and len(user_raw) <= 12 and _is_correct(user_raw, answer, zachet):
        return True
    # «зубного врача» при ответе «зубного»: лишние слова, взятые из текста вопроса, не мешают
    q_words = set(_tokens(question))
    q_stems = {_STEM.stemWord(w) for w in q_words} if _STEM else set()
    trimmed = [w for w in user if w not in q_words
               and not (_STEM and len(w) >= 4 and _STEM.stemWord(w) in q_stems)]
    return bool(trimmed) and trimmed != user and _is_correct(trimmed, answer, zachet)


def _is_correct(user: list[str], answer: str, zachet: str) -> bool:
    cands, flags = candidates(answer, zachet)
    for c in cands:
        right = _tokens(c)
        if _words_match(user, right, flags["any_order"]):
            return True
        # «мой», «я», «это» и т.п. игрок мог не писать — убираем их и из правильного ответа
        right_nf = [w for w in right if w not in FILLERS]
        if len(right_nf) >= 2 and right_nf != right and _words_match(user, right_nf, flags["any_order"]):
            return True
        # части ответа через запятую: «капуста, аист»
        if flags["any_order"] and "," in c:
            parts = [_tokens(p) for p in c.split(",")]
            flat = [w for p in parts for w in p]
            if _words_match(user, flat, True):
                return True
    if flags["meaning"]:
        key = [w for w in _tokens(answer) if len(w) > 3]
        if key and len(user) <= len(key) + 3 and all(any(word_ok(u, k) for u in user) for k in key):
            return True
    return False
