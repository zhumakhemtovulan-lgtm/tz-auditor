"""Верификатор цитат: аналог проверки аудиторских доказательств.

Модель обязана подкреплять каждое замечание дословной цитатой из документа.
Здесь код (без ИИ) проверяет, что цитата действительно есть в тексте,
и находит номера строк. Замечание без подтверждённой цитаты не попадает в отчёт.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .loader import Document

_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "'": '"', "’": '"',
                         "–": "-", "—": "-", "−": "-", "ё": "е", "Ё": "е", " ": " ",
                         " ": " ", " ": " "})


def norm(s: str) -> str:
    s = s.translate(_QUOTES).lower()
    s = re.sub(r"[*`_#>|\\]", " ", s)
    s = s.replace("…", " ").replace("...", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .;:,")


class QuoteIndex:
    def __init__(self, doc: Document):
        self.doc = doc
        self.norm_lines = [norm(ln) for ln in doc.lines]
        parts, self.starts = [], []
        pos = 0
        for nl in self.norm_lines:
            self.starts.append(pos)
            parts.append(nl)
            pos += len(nl) + 1
        self.full = " ".join(parts)

    def _line_at(self, offset: int) -> int:
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def _exact(self, q: str) -> list[int] | None:
        idx = self.full.find(q)
        if idx == -1:
            return None
        first, last = self._line_at(idx), self._line_at(idx + max(len(q) - 1, 0))
        return list(range(first, last + 1))

    def _fuzzy(self, q: str) -> tuple[list[int], float]:
        cands: list[tuple[float, int, list[int]]] = []
        n = len(self.norm_lines)
        for i in range(n):
            if not self.norm_lines[i]:
                continue
            for span in (1, 2, 3):
                j = min(n, i + span)
                window = " ".join(x for x in self.norm_lines[i:j] if x)
                if not window:
                    continue
                if len(q) < len(window):
                    m = SequenceMatcher(None, q, window, autojunk=False)
                    blk = m.find_longest_match(0, len(q), 0, len(window))
                    lo = max(0, blk.b - blk.a)
                    cand = window[lo: lo + len(q) + 10]
                    r = SequenceMatcher(None, q, cand, autojunk=False).ratio()
                else:
                    r = SequenceMatcher(None, q, window, autojunk=False).ratio()
                cands.append((r, span, list(range(i + 1, j + 1))))
        if not cands:
            return [], 0.0
        best = max(c[0] for c in cands)
        # Из почти равных совпадений берём самое короткое: меньше лишних строк в цитате.
        near = [c for c in cands if c[0] >= best - 0.03]
        near.sort(key=lambda c: (c[1], -c[0]))
        return near[0][2], best

    def find(self, quote: str) -> tuple[bool, list[int], str]:
        """Вернуть (найдено, строки, пояснение)."""
        q = norm(quote)
        if len(q) < 3:
            return False, [], "цитата пустая или слишком короткая"
        lines = self._exact(q)
        if lines:
            return True, lines, "точное совпадение"
        fragments = [norm(f) for f in re.split(r"\.\.\.|…|\[\.\.\.\]", quote)]
        fragments = [f for f in fragments if len(f) >= 8]
        if len(fragments) > 1:
            found = []
            for f in fragments:
                ln = self._exact(f)
                if not ln:
                    break
                found.extend(ln)
            else:
                return True, sorted(set(found)), "совпадение по фрагментам"
        if len(q) <= 400:
            lines, ratio = self._fuzzy(q)
            if ratio >= 0.80:
                return True, lines, f"приблизительное совпадение ({ratio:.0%}), цитата заменена текстом документа"
            return False, lines, f"цитата не найдена в документе (лучшее совпадение {ratio:.0%})"
        return False, [], "цитата не найдена в документе"


def verify_quotes(index: QuoteIndex, quotes: list[str]) -> tuple[bool, list[int], str]:
    """Все цитаты должны найтись. Возвращает итог, строки и пояснение."""
    if not quotes:
        return False, [], "нет цитаты из документа"
    all_lines: list[int] = []
    notes = []
    ok_all = True
    for q in quotes:
        ok, lines, note = index.find(q)
        all_lines.extend(lines)
        if not ok:
            ok_all = False
            notes.append(f"«{q[:80]}»: {note}")
    return ok_all, sorted(set(all_lines)), "; ".join(notes) if notes else "все цитаты найдены"


def clean_line(line: str) -> str:
    """Строка документа без разметки Markdown — для показа в цитате."""
    line = re.sub(r"\*\*|__|`", "", line)
    line = re.sub(r"^\s*(#+|>|[-*]\s)\s*", "", line)
    return line.strip()
