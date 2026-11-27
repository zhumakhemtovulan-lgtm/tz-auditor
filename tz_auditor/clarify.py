"""Уточнение по готовому отчёту: ответы автора ТЗ → пересмотр выводов → следующий раунд вопросов.

    python -m tz_auditor.clarify reports/report.json reports/report.answers.yaml reports/report_v2.md
    python -m tz_auditor.clarify reports/report_v2.json reports/report_v2.answers.yaml reports/report_v3.md

Берёт отчёт прошлого шага (JSON) и ответы автора (YAML). Делает до двух запросов к модели:
1) пересмотр: какие замечания закрыты ответами, как переписать требования;
2) следующий раунд: новые вопросы только по замечаниям, которые остались открытыми.
Выпускает новую версию отчёта и шаблон ответов на вопросы следующего раунда.
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
from .report import ReportWriter, write_reports
from .schemas import AuditResult


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(prog="python -m tz_auditor.clarify",
                                 description="Пересмотр выводов отчёта по ответам автора ТЗ.")
    ap.add_argument("report_json", help="JSON прошлого отчёта, например reports/report.json")
    ap.add_argument("answers", help="YAML с ответами (Q-01: ответ)")
    ap.add_argument("output", help="новый отчёт, например reports/report_v2.md")
    ap.add_argument("--document", help="путь к документу (по умолчанию из паспорта отчёта)")
    ap.add_argument("--no-followup", action="store_true", help="не задавать вопросы следующего раунда")
    ap.add_argument("--env", help="путь к .env")
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    try:
        result = AuditResult.model_validate(json.loads(Path(args.report_json).read_text("utf-8")))
        doc = load_document(args.document or result.passport.document_path)
        answered_before = {q.qid for q in result.questions if q.answer}
        answers = {k: v for k, v in _load_answers(args.answers).items() if k not in answered_before}
        if not answers:
            log("В файле нет новых ответов. Впишите ответы в кавычки после номеров вопросов.")
            return 1
        settings = load_settings(args.env)
        auditor = Auditor(settings, log=log)
        cur_round = max((q.round for q in result.questions), default=1)
        log(f"Раунд {cur_round}: новых ответов автора {len(answers)}. Модель: {settings.model}. Пересматриваю выводы…")
        new_log = auditor.clarify(doc, result.findings, result.questions, answers)
        for item in new_log:
            item.setdefault("round", cur_round)
        result.clarification_log = list(result.clarification_log) + new_log

        resolved = set(result.resolved_finding_ids)
        for item in result.clarification_log:
            resolved.update(x for x in (item.get("resolved_finding_ids") or []) if isinstance(x, str))
        result.resolved_finding_ids = sorted(resolved)
        open_f = [f for f in result.findings if f.fid not in resolved]
        sev = {f.severity for f in open_f}
        result.verdict_after_clarification = "нет" if "critical" in sev else "условно" if "major" in sev else "да"
        log(f"Закрыто ответами: {len(resolved)} из {len(result.findings)}; открыто: {len(open_f)}.")

        new_q = []
        unanswered = [q for q in result.questions if not q.answer]
        if open_f and not args.no_followup:
            log(f"Раунд {cur_round + 1}: готовлю вопросы по {len(open_f)} открытым замечаниям…")
            try:
                new_q = auditor.followup_questions(doc, open_f, result.questions, result.clarification_log,
                                                   cur_round + 1)
                result.questions = list(result.questions) + new_q
            except LLMError as exc:
                log(f"  ! вопросы следующего раунда не получены: {exc}")

        p = result.passport
        p.warnings = list(p.warnings) + [
            f"Раунд уточнения {cur_round} добавлен {datetime.now():%Y-%m-%d %H:%M} командой tz_auditor.clarify "
            f"(модель {auditor.llm.model_label()}); остальные разделы — из прогона {p.run_started}."]
        files = write_reports(result, doc, args.output)

        # Шаблон ответов следующего раунда: только вопросы без ответа.
        pending = [q for q in result.questions if not q.answer]
        ans_path = Path(args.output).with_suffix("")
        ans_path = Path(str(ans_path) + ".answers.yaml")
        tmp = result.model_copy(deep=True)
        tmp.questions = pending
        ans_path.write_text(ReportWriter(tmp, doc).answers_template().replace(
            "python -m tz_auditor <документ> <отчёт> --answers <этот файл>",
            f"python -m tz_auditor.clarify {Path(args.output).with_suffix('.json').as_posix()} "
            f"{ans_path.as_posix()} <новый отчёт>.md"), "utf-8")

        log(f"Итог: закрыто {len(resolved)} из {len(result.findings)} замечаний; открытых критичных: "
            f"{sum(f.severity == 'critical' for f in open_f)}; разработку начинать: {result.verdict_after_clarification}.")
        if new_q:
            log(f"Новых вопросов следующего раунда: {len(new_q)} — впишите ответы в {ans_path}")
        elif unanswered:
            log(f"Вопросов без ответа: {len(unanswered)} — их можно дописать в {ans_path}")
        for f in files:
            log(f"  {f}")
        return 0
    except (LoaderError, ConfigError, LLMError) as exc:
        log(f"ОШИБКА: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
