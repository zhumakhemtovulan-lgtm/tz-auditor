"""Оценка качества агента: сравнение отчёта с ручным эталонным ревью команды.

Эталон (gold.yaml) составляет команда вручную, до прогона агента. Агент эталон НЕ видит:
файл используется только здесь, чтобы измерить, сколько дефектов агент нашёл.

Запуск:
    python eval/eval.py reports/report.json eval/gold.yaml

Совпадение: замечание агента совпало с эталонным дефектом, если у них пересекаются номера строк
(допуск ±1 строка). Показывает полноту (recall) и список пропущенных дефектов.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    report = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    gold = yaml.safe_load(Path(sys.argv[2]).read_text("utf-8")) or []
    findings = report.get("findings", [])

    def near(a: list[int], b: list[int]) -> bool:
        return any(abs(x - y) <= 1 for x in a for y in b)

    found, missed = [], []
    used = set()
    for g in gold:
        g_lines = g.get("lines", [])
        hits = [f["fid"] for f in findings if near(f.get("lines", []), g_lines)]
        if hits:
            found.append((g, hits))
            used.update(hits)
        else:
            missed.append(g)

    n = len(gold)
    crit = [g for g in gold if g.get("severity") == "critical"]
    crit_found = [g for g, _ in found if g.get("severity") == "critical"]
    print(f"Эталонных дефектов: {n}; найдено агентом: {len(found)} "
          f"(полнота {len(found) / n:.0%})" if n else "Эталон пуст")
    if crit:
        print(f"Критичных в эталоне: {len(crit)}; найдено: {len(crit_found)} ({len(crit_found) / len(crit):.0%})")
    extra = [f for f in findings if f["fid"] not in used]
    print(f"Замечаний агента вне эталона: {len(extra)} (проверьте вручную: ошибка агента или пропуск эталона)")
    print("\nНайдено:")
    for g, hits in found:
        print(f"  ✓ {g.get('id')}: {g.get('title')} → {', '.join(hits)}")
    print("\nПропущено:")
    for g in missed:
        print(f"  ✗ {g.get('id')}: {g.get('title')} (строки {g.get('lines')})")
    print("\nВне эталона:")
    for f in extra:
        print(f"  ? {f['fid']}: {f['title']} (строки {f.get('lines')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
