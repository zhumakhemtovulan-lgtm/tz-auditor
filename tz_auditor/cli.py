"""Запуск агента из командной строки.

    python -m tz_auditor docs/requirements.md reports/report.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .config import ConfigError, load_settings
from .llm import LLMError
from .loader import LoaderError
from .pipeline import Auditor
from .report import write_reports


def _load_answers(path: str) -> dict[str, str]:
    import yaml

    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Файл с ответами не найден: {p}")
    data = yaml.safe_load(p.read_text("utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit("Файл с ответами должен содержать пары «Q-01: ответ».")
    return {str(k): str(v) for k, v in data.items() if v not in (None, "")}


def _ask_console(questions):
    print("\n=== Уточняющие вопросы агента ===")
    print("Ответьте своими словами или номером варианта. Enter — пропустить, q — закончить.\n")
    answers = {}
    for q in questions:
        if q.priority == "low":
            continue
        print(f"[{q.qid}] {q.question}")
        for o in q.options:
            print(f"    {o}")
        if q.why:
            print(f"    (почему важно: {q.why})")
        try:
            a = input("  Ответ > ").strip()
        except EOFError:
            break
        if a.lower() in {"q", "й", "quit", "exit"}:
            break
        if a:
            answers[q.qid] = a
        print()
    return answers


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(
        prog="python -m tz_auditor",
        description="ТЗ-Аудитор: проверка документа с требованиями по ISO/IEC/IEEE 29148 и IEEE 830.",
    )
    ap.add_argument("input", help="путь к документу с требованиями (PDF, DOCX, MD, TXT, HTML)")
    ap.add_argument("output", help="путь к файлу отчёта (.md или .html); рядом появятся .html, .json, .answers.yaml")
    ap.add_argument("--answers", help="YAML-файл с ответами на уточняющие вопросы (Q-01: ответ)")
    ap.add_argument("--interactive", "-i", action="store_true",
                    help="задать уточняющие вопросы в консоли и пересмотреть выводы по ответам")
    ap.add_argument("--provider", help="gemini | openai | ollama (по умолчанию из .env)")
    ap.add_argument("--model", help="модель (по умолчанию из .env)")
    ap.add_argument("--no-cache", action="store_true", help="не использовать кэш ответов модели")
    ap.add_argument("--env", help="путь к .env (по умолчанию ./.env)")
    args = ap.parse_args(argv)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    log_path = out.with_suffix(".log")
    log_file = log_path.open("w", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    try:
        settings = load_settings(args.env, args.provider, args.model, use_cache=not args.no_cache)
        log(f"ТЗ-Аудитор: документ {args.input}, модель {settings.model} ({settings.provider})")
        answers = _load_answers(args.answers) if args.answers else None
        auditor = Auditor(settings, log=log)
        result, doc = auditor.run(args.input, answers=answers,
                                  ask=_ask_console if args.interactive else None)
        files = write_reports(result, doc, args.output)
        log("Готово. Файлы отчёта:")
        for f in files:
            log(f"  {f}")
        log(f"  {log_path} (журнал прогона)")
        v = result.verdict
        log(f"Итог: требований {len(result.requirements)}, замечаний {len(result.findings)}, "
            f"разработка: {v.ready_for_development}, тестирование: {v.ready_for_testing}.")
        return 0
    except LoaderError as exc:
        log(f"ОШИБКА ЧТЕНИЯ ДОКУМЕНТА: {exc}")
        return 2
    except ConfigError as exc:
        log(f"ОШИБКА НАСТРОЙКИ: {exc}")
        return 3
    except LLMError as exc:
        log(f"ОШИБКА МОДЕЛИ: {exc}")
        log("Подсказка: повторите запуск — уже полученные ответы модели сохранены в кэше и не будут запрошены снова.")
        return 4
    except KeyboardInterrupt:
        log("Прервано пользователем.")
        return 130
    finally:
        log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
