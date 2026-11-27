"""Уточнение по готовому отчёту: ответы автора ТЗ → пересмотр выводов.

    python -m tz_auditor.clarify reports/report.json reports/report.answers.yaml reports/report_v2.md

Берёт отчёт прошлого прогона (JSON) и ответы автора на уточняющие вопросы (YAML),
делает один запрос к модели и выпускает новую версию отчёта: какие дефекты закрыты ответами,
как переписать требования, можно ли теперь начинать разработку.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .cli import _load_answers
from .config import ConfigError, load_settings
from .llm import LLMError
from .loader import LoaderError, load_document
from .pipeline import Auditor
from .report import write_reports
from .schemas import SEVERITY_ORDER, AuditResult


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(prog="python -m tz_auditor.clarify",
                                 description="Пересмотр выводов отчёта по ответам автора ТЗ.")
    ap.add_argument("report_json", help="JSON прошлого отчёта, например reports/report.json")
    ap.add_argument("answers", help="YAML с ответами (Q-01: ответ), например reports/report.answers.yaml")
    ap.add_argument("output", help="новый отчёт, например reports/report_v2.md")
    ap.add_argument("--document", help="путь к документу (по умолчанию из паспорта отчёта)")
    ap.add_argument("--env", help="путь к .env")
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    try:
        result = AuditResult.model_validate(json.loads(Path(args.report_json).read_text("utf-8")))
        doc = load_document(args.document or result.passport.document_path)
        answers = _load_answers(args.answers)
        if not answers:
            log("В файле ответов нет ни одного ответа. Впишите ответы в кавычки после Q-01: и т. д.")
            return 1
        settings = load_settings(args.env)
        auditor = Auditor(settings, log=log)
        log(f"Ответов автора: {len(answers)}. Модель: {settings.model}. Пересматриваю выводы…")
        result.clarification_log = auditor.clarify(doc, result.findings, result.questions, answers)

        resolved = set()
        for item in result.clarification_log:
            resolved.update(x for x in (item.get("resolved_finding_ids") or []) if isinstance(x, str))
        result.resolved_finding_ids = sorted(resolved)
        open_f = [f for f in result.findings if f.fid not in resolved]
        sev = {f.severity for f in open_f}
        result.verdict_after_clarification = "нет" if "critical" in sev else "условно" if "major" in sev else "да"

        p = result.passport
        p.warnings = list(p.warnings) + [
            f"Раздел «Ответы автора и пересмотр выводов» добавлен {datetime.now():%Y-%m-%d %H:%M} "
            f"командой tz_auditor.clarify (модель {auditor.llm.model_label()}); остальные разделы — из прогона {p.run_started}."]
        files = write_reports(result, doc, args.output)
        crit_open = sum(f.severity == "critical" for f in open_f)
        log(f"Закрыто ответами: {len(resolved)} из {len(result.findings)} замечаний; "
            f"открытых критичных: {crit_open}; разработку начинать после уточнения: {result.verdict_after_clarification}.")
        for f in files:
            log(f"  {f}")
        return 0
    except (LoaderError, ConfigError, LLMError) as exc:
        log(f"ОШИБКА: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
