"""Автоматические проверки без ИИ (линтер требований).

Правила ищут типичные дефекты по шаблонам: размытые слова, незаполненные поля,
смешение единиц, сроки без измеримого критерия. Результат воспроизводим и бесплатен.
Эти сигналы показываются в отчёте отдельно и передаются модели как подсказки.
"""

from __future__ import annotations

import re

from .loader import Document
from .schemas import Finding

WEAK_WORDS = [
    r"быстр\w*", r"своевременн\w*", r"удобн\w*", r"интуитивн\w*", r"при необходимости",
    r"по возможности", r"и т\.\s?д\.?", r"и т\.\s?п\.?", r"и/или", r"оптимальн\w*",
    r"достаточн\w*", r"как правило", r"обычно", r"примерно", r"приблизительно",
    r"максимально", r"эффективн\w*", r"надё?жн\w*", r"современн\w*", r"гибк\w*",
    r"мгновенн\w*", r"в кратчайш\w*", r"легк\w*", r"понятн\w*", r"корректн\w*",
    r"user[- ]friendly", r"\betc\b", r"asap", r"и прочее", r"и др\.",
]
PLACEHOLDERS = [
    r"\bTBD\b", r"\bTODO\b", r"\bTBA\b", r"заполня\w+ (позже|тестировщик\w*|аналитик\w*)",
    r"будет уточнен\w*", r"уточнить позже", r"\?\?\?", r"не заполнен\w*",
]
UNIT_PAIRS = [("тиын", r"тенге|₸|\bkzt\b"), ("копе", r"рубл|₽"), ("цент", r"доллар|\$")]
TIME_RE = re.compile(
    r"в течение\s+(\d+\s*)?(мс|миллисекунд\w*|секунд\w*|минут\w*|час\w*)|не (более|позднее)\s+\d+\s*(мс|с|сек\w*|мин\w*)",
    re.I,
)


def _in_code(lines: list[str]) -> set[int]:
    inside, res = False, set()
    for i, ln in enumerate(lines, 1):
        if ln.strip().startswith("```"):
            inside = not inside
            res.add(i)
            continue
        if inside:
            res.add(i)
    return res


def _mk(code: str, criterion: str, severity: str, title: str, description: str,
        hits: list[tuple[int, str]], recommendation: str, limit: int = 6) -> Finding:
    quotes = [q for _, q in hits[:limit]]
    lines = [n for n, _ in hits]
    more = f" Всего совпадений: {len(hits)}." if len(hits) > limit else ""
    return Finding(
        scope="set", criterion=criterion, severity=severity, title=f"[{code}] {title}",
        description=description + more, quotes=quotes, lines=lines,
        recommendation=recommendation, source="rule", verified=True,
        verification_note="найдено правилом по тексту документа", critic_verdict="unchecked",
    )


def run_rules(doc: Document) -> list[Finding]:
    lines = doc.lines
    code_lines = _in_code(lines)
    out: list[Finding] = []

    weak_hits = []
    weak_re = re.compile("|".join(WEAK_WORDS), re.I)
    for i, ln in enumerate(lines, 1):
        if i in code_lines:
            continue
        for m in weak_re.finditer(ln):
            weak_hits.append((i, ln.strip()))
            break
    if weak_hits:
        out.append(_mk("RULE-WEAK", "UNAMBIGUOUS", "minor", "Размытые формулировки",
                       "Найдены слова без измеримого значения. Каждое нужно проверить: можно ли "
                       "понять его двумя способами и как проверить тестом.", weak_hits,
                       "Заменить размытые слова числом, условием или определением."))

    ph_re = re.compile("|".join(PLACEHOLDERS), re.I)
    ph_hits = [(i, ln.strip()) for i, ln in enumerate(lines, 1) if ph_re.search(ln)]
    if ph_hits:
        out.append(_mk("RULE-TBD", "SET_COMPLETE", "major", "Незаполненные разделы и заглушки",
                       "В документе есть пометки о том, что содержание будет добавлено позже.",
                       ph_hits, "Заполнить разделы до передачи в разработку."))

    yn_hits = [(i, ln.strip()) for i, ln in enumerate(lines, 1)
               if re.search(r"Да\s*/\s*Нет", ln)]
    if yn_hits:
        out.append(_mk("RULE-CHECKLIST", "CONFORMING", "minor", "Варианты «Да / Нет» без выбора",
                       f"В {len(yn_hits)} строках остались варианты ответа шаблона без выбора: "
                       "чек-лист не заполнен.", yn_hits, "Заполнить чек-лист и проставить отметки.", limit=3))

    dash_hits = []
    for i, ln in enumerate(lines, 1):
        if "|" in ln and not re.match(r"^\s*\|?\s*:?-{3,}", ln):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) >= 2 and any(c in {"—", "-", "–"} for c in cells):
                dash_hits.append((i, ln.strip()))
        elif re.search(r":\s*[—–]\s*(\.|$)", ln):
            dash_hits.append((i, ln.strip()))
    empty_rows = [(i, ln.strip()) for i, ln in enumerate(lines, 1)
                  if "|" in ln and re.fullmatch(r"\|(\s*\|)+\s*", ln.strip())]
    if dash_hits or empty_rows:
        out.append(_mk("RULE-EMPTY", "SET_COMPLETE", "minor", "Пустые поля в таблицах",
                       "Поля шаблона заполнены прочерком или пустые строки таблиц.",
                       dash_hits + empty_rows, "Заполнить поля или явно указать «не применяется» с причиной."))

    for sub, main in UNIT_PAIRS:
        sub_hits = [(i, ln.strip()) for i, ln in enumerate(lines, 1) if re.search(sub, ln, re.I)]
        main_hits = [(i, ln.strip()) for i, ln in enumerate(lines, 1) if re.search(main, ln, re.I)]
        if sub_hits and main_hits:
            out.append(_mk("RULE-UNITS", "SET_CONSISTENT", "major", "Суммы в разных единицах",
                           f"Суммы встречаются и в основной единице, и в дробной («{sub}…»). "
                           "Нужно проверить, описан ли пересчёт при сравнении сумм.",
                           sub_hits + main_hits[:2], "Указать единицу хранения и сравнения и правило пересчёта."))

    time_hits = []
    for i, ln in enumerate(lines, 1):
        if TIME_RE.search(ln) and not re.search(r"%|перцентил|p9\d|процент", ln, re.I):
            time_hits.append((i, ln.strip()))
    if time_hits:
        out.append(_mk("RULE-NFR", "VERIFIABLE", "major", "Срок без измеримого критерия",
                       "Срок указан без доли случаев (перцентиля), условий нагрузки или явной точки отсчёта.",
                       time_hits, "Указать точку отсчёта и окончания, перцентиль (например, 95%) и нагрузку."))

    if not any(re.search(r"верси[яи]\s*[:№]?\s*v?\d", ln, re.I) for ln in lines):
        vhits = [(i, ln.strip()) for i, ln in enumerate(lines, 1) if re.search(r"верси", ln, re.I)]
        out.append(_mk("RULE-VERSION", "MODIFIABLE", "minor", "Не указана версия документа",
                       "Номер версии документа не найден.",
                       vhits or [(1, lines[0].strip())], "Указать версию, дату и историю изменений."))

    return out
