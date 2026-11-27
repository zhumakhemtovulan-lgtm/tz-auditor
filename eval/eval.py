"""Оценка качества агента: сравнение отчётов с эталонным ручным ревью (eval-driven development).

Эталон (gold.yaml) составлен и проверен командой вручную. Агент этот файл НЕ видит:
он используется только здесь, чтобы измерить качество.

Запуск:
    python eval/eval.py eval/gold.yaml reports/report.json
    python eval/eval.py eval/gold.yaml reports/report.json reports/run2/report.json reports/test/report.json

Показывает:
  - полноту (recall): какая доля эталонных дефектов найдена в каждом прогоне;
  - стабильность (pass^k): какие дефекты найдены во ВСЕХ прогонах;
  - замечания агента вне эталона (на ручную проверку: ошибка агента или пропуск эталона).

Совпадение: у замечания агента и эталонного дефекта пересекаются номера строк (допуск ±1)
и в ЗАГОЛОВКЕ замечания (или коде правила) есть хотя бы одно ключевое слово эталона.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


def near(a: list[int], b: list[int]) -> bool:
    return any(abs(x - y) <= 1 for x in a for y in b)


def matches(g: dict, f: dict) -> bool:
    if not near(f.get("lines", []), g.get("lines", [])):
        return False
    kws = [k.lower() for k in g.get("keywords", [])]
    if not kws:
        return True
    # Строго: ключевое слово должно быть в заголовке замечания или коде правила,
    # а не где-то в описании — иначе засчитываются чужие замечания с соседними строками.
    text = (str(f.get("title", "")) + " " + str(f.get("rule", ""))).lower()
    return any(k in text for k in kws)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    gold = yaml.safe_load(Path(sys.argv[1]).read_text("utf-8")) or []
    runs = []
    for p in sys.argv[2:]:
        r = json.loads(Path(p).read_text("utf-8"))
        runs.append((p, r.get("passport", {}).get("model", "?"), r.get("findings", [])))

    print(f"Эталон: {len(gold)} дефектов (критичных: {sum(g.get('severity') == 'critical' for g in gold)}). "
          f"Прогонов: {len(runs)}.\n")
    header = "| Эталонный дефект | " + " | ".join(f"Прогон {i}" for i in range(1, len(runs) + 1)) + " |"
    print(header)
    print("|" + " --- |" * (len(runs) + 1))
    found_counts = [0] * len(runs)
    stable = 0
    used = [set() for _ in runs]
    for g in gold:
        cells = []
        hits_all = True
        for i, (_, _, findings) in enumerate(runs):
            hits = [f["fid"] for f in findings if matches(g, f)]
            if hits:
                found_counts[i] += 1
                used[i].update(hits)
                cells.append("✅ " + ", ".join(hits[:2]))
            else:
                hits_all = False
                cells.append("❌")
        stable += hits_all
        crit = " **(крит.)**" if g.get("severity") == "critical" else ""
        print(f"| {g.get('id')}: {g.get('title')}{crit} | " + " | ".join(cells) + " |")

    n = len(gold) or 1
    print("\n**Полнота по прогонам:**")
    for i, (p, model, findings) in enumerate(runs):
        crit_total = [g for g in gold if g.get("severity") == "critical"]
        print(f"- Прогон {i + 1} (`{p}`, {model}): найдено {found_counts[i]} из {len(gold)} "
              f"({found_counts[i] / n:.0%}); замечаний вне эталона: {len([f for f in findings if f['fid'] not in used[i]])}")
    if len(runs) > 1:
        print(f"\n**Стабильность (pass^{len(runs)}):** {stable} из {len(gold)} дефектов найдены во всех прогонах "
              f"({stable / n:.0%}).")
    crit_ids = [g for g in gold if g.get("severity") == "critical"]
    if crit_ids:
        crit_ok = sum(all(any(matches(g, f) for f in fs) for _, _, fs in runs) for g in crit_ids)
        print(f"**Критичные дефекты во всех прогонах:** {crit_ok} из {len(crit_ids)}.")
    print("\n**Замечания агента вне эталона** (проверить вручную):")
    for i, (_, _, findings) in enumerate(runs):
        extra = [f for f in findings if f["fid"] not in used[i]]
        for f in extra:
            print(f"- Прогон {i + 1}, {f['fid']}: {f['title'][:100]} (строки {f.get('lines', [])[:6]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
