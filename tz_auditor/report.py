"""Формирование отчёта: Markdown (основной), HTML (для печати в PDF) и JSON (для машин)."""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

from .loader import Document
from .schemas import SEVERITY_RU, AuditResult, Finding
from .verify import QuoteIndex

CRIT_RU = {
    "NECESSARY": "Необходимость", "APPROPRIATE": "Уместность", "UNAMBIGUOUS": "Однозначность",
    "COMPLETE": "Полнота", "SINGULAR": "Единичность", "FEASIBLE": "Выполнимость",
    "VERIFIABLE": "Проверяемость", "CORRECT": "Корректность", "CONFORMING": "Соответствие шаблону",
    "SET_COMPLETE": "Полнота набора", "SET_CONSISTENT": "Непротиворечивость набора",
    "SET_FEASIBLE": "Выполнимость набора", "SET_COMPREHENSIBLE": "Понятность набора",
    "SET_VALIDATABLE": "Возможность валидации", "RANKED": "Ранжированность (IEEE 830)",
    "MODIFIABLE": "Модифицируемость (IEEE 830)", "TRACEABLE": "Прослеживаемость (IEEE 830)",
}
SHORT = ["NECESSARY", "APPROPRIATE", "UNAMBIGUOUS", "COMPLETE", "SINGULAR", "FEASIBLE",
         "VERIFIABLE", "CORRECT", "CONFORMING"]
SHORT_RU = ["Необх.", "Умест.", "Однозн.", "Полн.", "Един.", "Выполн.", "Провер.", "Корр.", "Шабл."]
STATUS_SYM = {"ok": "✓", "issue": "✗", "n/a": "—"}
VERDICT_RU = {"ok": "в порядке", "needs_fix": "исправить", "blocker": "блокер"}
PRIO_RU = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}
SCEN_RU = {"ok": "без замечаний", "conflict": "КОНФЛИКТ", "gap": "пробел"}

CATEGORY = [
    ("Противоречивые", {"SET_CONSISTENT", "CORRECT"}, {"FIN-CONFLICT", "API-STATUS", "API-CONTRACT"}),
    ("Непроверяемые", {"VERIFIABLE", "SET_VALIDATABLE"}, {"NFR-MEASURABLE"}),
    ("Неполные", {"COMPLETE", "SET_COMPLETE"}, set()),
    ("Неоднозначные", {"UNAMBIGUOUS", "SET_COMPREHENSIBLE"}, set()),
]


def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def category_of(f: Finding) -> str:
    for name, crits, rules in CATEGORY:
        if f.criterion in crits or f.rule in rules:
            return name
    return "Прочие"


class ReportWriter:
    def __init__(self, result: AuditResult, doc: Document):
        self.r = result
        self.doc = doc
        self.index = QuoteIndex(doc)
        self.req_by_id = {q.rid: q for q in result.requirements}

    def _loc(self, lines: list[int]) -> str:
        if not lines:
            return "—"
        if len(lines) > 6:
            lines = lines[:6]
            tail = " …"
        else:
            tail = ""
        if self.doc.page_of_line:
            return ", ".join(self.doc.locate(n) for n in lines) + tail
        return "строки " + ", ".join(str(n) for n in lines) + tail if len(lines) > 1 else f"строка {lines[0]}"

    def _req_ref(self, rids: list[str]) -> str:
        parts = []
        for rid in rids:
            r = self.req_by_id.get(rid)
            if r and r.doc_id:
                parts.append(f"{rid} {r.doc_id}" if r.doc_id.startswith("(") else f"{rid} ({r.doc_id})")
            else:
                parts.append(rid)
        return ", ".join(parts) if parts else "весь документ"

    def _finding_md(self, f: Finding, level: str = "####") -> list[str]:
        out = [f"{level} {f.fid} · {SEVERITY_RU[f.severity]} · {f.title}", ""]
        crit = CRIT_RU.get(f.criterion, f.criterion)
        rule = f" · правило {f.rule}" if f.rule else ""
        out.append(f"- **Требования:** {self._req_ref(f.requirement_ids)}")
        out.append(f"- **Критерий:** {crit} (`{f.criterion}`){rule}")
        out.append(f"- **Где:** {self._loc(f.lines)}")
        out.append("- **Цитаты из документа:**")
        for q in f.quotes:
            _, ql, _ = self.index.find(q)
            ql = [i for i in ql if self.doc.lines[i - 1].strip()]
            loc = f" — _{self._loc(ql)}_" if ql else ""
            out.append(f"  > «{q.strip()}»{loc}")
        out.append(f"- **Проблема:** {f.description}")
        if f.impact:
            out.append(f"- **Чем опасно:** {f.impact}")
        if f.recommendation:
            out.append(f"- **Как исправить:** {f.recommendation}")
        if f.suggested_rewrite:
            out.append(f"- **Предлагаемая формулировка:** _{f.suggested_rewrite}_")
        check = f"цитаты {f.verification_note}"
        if f.critic_verdict == "confirmed":
            check += f"; критик: подтверждено ({f.critic_note})" if f.critic_note else "; критик: подтверждено"
        out.append(f"- **Проверка замечания:** {check}")
        out.append("")
        return out

    # ------------------------------------------------------------------
    def markdown(self) -> str:
        r, p = self.r, self.r.passport
        L: list[str] = []
        sev = Counter(f.severity for f in r.findings)
        L += [f"# Отчёт о проверке требований: {p.document_title or p.document_name}", "",
              "> Отчёт сформирован автоматически агентом **ТЗ-Аудитор** по результатам прогона. "
              "Каждое замечание подтверждено дословной цитатой из документа (проверено кодом) "
              "и просмотрено независимым шагом-критиком.", ""]

        # 1. Паспорт
        L += ["## 1. Паспорт прогона", "", "| Поле | Значение |", "| :--- | :--- |",
              f"| Документ | {_cell(p.document_title)} |",
              f"| Файл | `{p.document_name}` ({p.document_format}) |",
              f"| Контрольная сумма SHA-256 | `{p.document_sha256[:16]}…` |",
              f"| Дата и время прогона | {p.run_started} |",
              f"| Длительность прогона | {p.duration_sec:.0f} с ({p.duration_sec / 60:.1f} мин) |",
              f"| Модель | {p.model} (провайдер: {p.provider}) |",
              f"| Версия агента | {p.agent_version} |",
              f"| Обращений к модели | {p.llm_calls} (из кэша: {p.cache_hits}) |",
              f"| Токены (вход / выход) | {p.tokens_in:,} / {p.tokens_out:,} |".replace(",", " "),
              f"| Методика | ISO/IEC/IEEE 29148:2018 (5.2.5, 5.2.6), IEEE Std 830-1998 (4.3), предметные правила `methodology/` |",
              ""]
        if p.warnings:
            L += ["**Предупреждения при чтении файла:**", ""] + [f"- {w}" for w in p.warnings] + [""]

        v = r.verdict
        L += ["**Коротко.** "
              f"Требований: **{len(r.requirements)}**. Подтверждённых замечаний: **{len(r.findings)}** "
              f"(критичных {sev['critical']}, существенных {sev['major']}, незначительных {sev['minor']}). "
              f"Начинать разработку: **{v.ready_for_development}**, тестирование: **{v.ready_for_testing}**. "
              f"Уточняющих вопросов к автору: **{len(r.questions)}**.", ""]

        # 2. Нарезка
        L += ["## 2. Как агент нарезал требования", "",
              "Идентификатор R-xx присвоен агентом. Если в документе номера не было, во второй колонке "
              "указано место в скобках, а цитата сохранена.", "",
              "| ID в отчёте | ID в документе | Тип | Где | Краткая формулировка | Цитата |",
              "| :--- | :--- | :--- | :--- | :--- | :--- |"]
        for q in r.requirements:
            quote = q.quote if len(q.quote) <= 160 else q.quote[:157] + "…"
            L.append(f"| {q.rid} | {_cell(q.doc_id) or '—'} | {_cell(q.kind)} | {self._loc(q.lines)} | "
                     f"{_cell(q.summary)} | «{_cell(quote)}» |")
        L.append("")

        # 3. Проверка каждого требования
        L += ["## 3. Проверка каждого требования", "",
              "Матрица оценки по свойствам отдельного требования (ISO/IEC/IEEE 29148, 5.2.5). "
              "✓ — без замечаний, ✗ — есть замечание, — — неприменимо.", "",
              "| ID | " + " | ".join(SHORT_RU) + " | Итог |",
              "| :--- | " + " | ".join([":---:"] * len(SHORT)) + " | :--- |"]
        ass = {a.rid: a for a in r.assessments}
        for q in r.requirements:
            a = ass.get(q.rid)
            sc = {s.criterion: s for s in (a.scores if a else [])}
            cells = [STATUS_SYM.get(sc[c].status, "·") if c in sc else "·" for c in SHORT]
            L.append(f"| {q.rid} | " + " | ".join(cells) + f" | {VERDICT_RU[a.verdict] if a else '—'} |")
        L.append("")

        by_req: dict[str, list[Finding]] = {}
        for f in r.findings:
            if f.scope == "requirement":
                for rid in f.requirement_ids[:1]:
                    by_req.setdefault(rid, []).append(f)
        L += ["### Замечания по требованиям", ""]
        for q in r.requirements:
            fs = by_req.get(q.rid)
            a = ass.get(q.rid)
            issues = [s for s in (a.scores if a else []) if s.status == "issue"]
            if not fs and not issues:
                continue
            L += [f"### {q.rid} · {q.doc_id or q.kind}: {q.summary}", "",
                  f"> «{q.quote.strip()}» — _{self._loc(q.lines)}_", ""]
            if issues:
                L.append("Оценка по критериям: " + "; ".join(
                    f"**{CRIT_RU.get(s.criterion, s.criterion)}** — {s.comment}" for s in issues))
                L.append("")
            for f in fs or []:
                L += self._finding_md(f)
        ok_ids = [q.rid for q in r.requirements if ass.get(q.rid) and ass[q.rid].verdict == "ok"
                  and not any(s.status == "issue" for s in ass[q.rid].scores)]
        if ok_ids:
            L += [f"Без замечаний: {', '.join(ok_ids)}.", ""]

        # 4. Набор
        L += ["## 4. Проверка всего набора требований", ""]
        if r.set_summary:
            L += [r.set_summary, ""]
        if r.set_scores:
            L += ["| Свойство набора | Оценка | Комментарий |", "| :--- | :---: | :--- |"]
            for s in r.set_scores:
                L.append(f"| {CRIT_RU.get(s.criterion, s.criterion)} | {STATUS_SYM.get(s.status, '·')} | {_cell(s.comment)} |")
            L.append("")
        set_f = [f for f in r.findings if f.scope == "set"]
        if set_f:
            L += ["### Замечания уровня набора", ""]
            for f in set_f:
                L += self._finding_md(f)
        if r.scenarios:
            L += ["### Сценарный прогон", "",
                  "Агент «прогнал» пограничные сценарии через правила документа, чтобы найти конфликты и пробелы.", "",
                  "| # | Сценарий | Какие пункты применяются | Что следует из документа | Итог |",
                  "| :--- | :--- | :--- | :--- | :--- |"]
            for i, s in enumerate(r.scenarios, 1):
                note = f" {s.note}" if s.note else ""
                L.append(f"| {i} | {_cell(s.scenario)} | {_cell(self._req_ref(s.rules_applied))} | "
                         f"{_cell(s.outcome)} | **{SCEN_RU[s.status]}**.{_cell(note)} |")
            L.append("")

        # 5. Вопросы
        L += ["## 5. Уточняющие вопросы к автору ТЗ", "",
              "Вопросы, без ответа на которые нельзя однозначно реализовать или протестировать требования. "
              "Ответы можно вписать в файл `*.answers.yaml` рядом с отчётом и запустить агента повторно "
              "с флагом `--answers`: агент пересмотрит выводы.", ""]
        for q in r.questions:
            L += [f"**{q.qid} · {PRIO_RU[q.priority]} приоритет · кому: {q.addressee}**", "", q.question, ""]
            if q.options:
                L += ["Варианты ответа: " + "; ".join(q.options), ""]
            if q.why:
                L += [f"_Почему важно:_ {q.why}", ""]
            refs = ", ".join(q.finding_ids + [self._req_ref(q.requirement_ids)] if q.requirement_ids else q.finding_ids)
            if refs:
                L += [f"_Связано:_ {refs}", ""]

        if r.clarification_log:
            L += ["### Ответы автора и пересмотр выводов", "",
                  "| Вопрос | Ответ | Что меняется | Закрытые замечания | Новая формулировка |",
                  "| :--- | :--- | :--- | :--- | :--- |"]
            for c in r.clarification_log:
                L.append(f"| {c.get('qid', '')} | {_cell(str(c.get('answer', '')))} | {_cell(str(c.get('effect', '')))} | "
                         f"{', '.join(c.get('resolved_finding_ids', []) or []) or '—'} | {_cell(str(c.get('proposed_text', '')))} |")
            L.append("")

        # 6. Чек-лист
        if r.checklist:
            L += ["## 6. Чек-лист ревью требований (заполнен агентом)", "",
                  "| Пункт чек-листа | Оценка агента | Обоснование | Замечания |", "| :--- | :---: | :--- | :--- |"]
            for c in r.checklist:
                L.append(f"| {_cell(c.item)} | {c.status} | {_cell(c.justification)} | {', '.join(c.finding_ids) or '—'} |")
            L.append("")

        # 7. Сводка
        L += ["## 7. Сводка", "", "| Показатель | Значение |", "| :--- | ---: |",
              f"| Требований выделено | {len(r.requirements)} |",
              f"| Требований с замечаниями | {sum(1 for a in r.assessments if a.verdict != 'ok')} |",
              f"| Подтверждённых нарушений | {len(r.findings)} |",
              f"| из них критичных | {sev['critical']} |",
              f"| из них существенных | {sev['major']} |",
              f"| из них незначительных | {sev['minor']} |",
              f"| Сигналов автоматических правил | {len(r.rule_signals)} |",
              f"| Отклонено замечаний (верификатор и критик) | {len(r.rejected_findings)} |",
              f"| Уточняющих вопросов (высокий приоритет) | {len(r.questions)} ({sum(q.priority == 'high' for q in r.questions)}) |",
              ""]
        cats = Counter(category_of(f) for f in r.findings)
        L += ["**Нарушения по типам:** " + ", ".join(f"{k} — {v}" for k, v in cats.most_common()) + ".", ""]
        crit = [f for f in r.findings if f.severity == "critical"]
        L += ["### Критичные дефекты", ""]
        if crit:
            L += ["| ID | Тип | Дефект | Требования | Где |", "| :--- | :--- | :--- | :--- | :--- |"]
            for f in crit:
                L.append(f"| {f.fid} | {category_of(f)} | {_cell(f.title)} | {_cell(self._req_ref(f.requirement_ids))} | {self._loc(f.lines)} |")
        else:
            L.append("Критичных дефектов не найдено.")
        L.append("")
        major = [f for f in r.findings if f.severity == "major"]
        if major:
            L += ["### Существенные дефекты", "", "| ID | Тип | Дефект | Где |", "| :--- | :--- | :--- | :--- |"]
            for f in major:
                L.append(f"| {f.fid} | {category_of(f)} | {_cell(f.title)} | {self._loc(f.lines)} |")
            L.append("")

        # 8. Итог
        L += ["## 8. Итог", "",
              f"**Можно ли начинать разработку:** {v.ready_for_development}. "
              f"**Можно ли начинать тестирование (тест-дизайн):** {v.ready_for_testing}.", ""]
        if v.summary:
            L += [v.summary, ""]
        L += ["Правило решения: есть критичный дефект → «нет»; есть только существенные → «условно»; "
              "только незначительные → «да» (`methodology/severity.md`).", ""]
        if v.top_fixes:
            L += ["**Что исправить в первую очередь:**", ""] + [f"{i}. {x}" for i, x in enumerate(v.top_fixes, 1)] + [""]

        # Приложения
        L += ["## Приложение А. Автоматические проверки без ИИ", "",
              "Правила-шаблоны (`tz_auditor/rules.py`) работают без модели и дают одинаковый результат при каждом запуске.", ""]
        for f in r.rule_signals:
            L += [f"**{f.title}** ({SEVERITY_RU[f.severity]}, {self._loc(f.lines)}). {f.description}", ""]
            for q in f.quotes[:3]:
                L.append(f"> {q}")
            L.append("")
        L += ["## Приложение Б. Отклонённые замечания", "",
              "Замечания, которые агент нашёл, но не включил в отчёт: цитата не подтвердилась "
              "в тексте или критик признал их ложными или дублями. Показаны для прозрачности.", ""]
        if r.rejected_findings:
            L += ["| ID | Замечание | Причина отклонения |", "| :--- | :--- | :--- |"]
            for f in r.rejected_findings:
                L.append(f"| {f.fid} | {_cell(f.title)} | {_cell(f.critic_note or f.verification_note)} |")
        else:
            L.append("Нет.")
        L += ["", "---", f"_ТЗ-Аудитор {p.agent_version}. Модель {p.model}. {p.run_finished}._", ""]
        return "\n".join(L)

    def html(self, md_text: str) -> str:
        try:
            import markdown

            body = markdown.markdown(md_text, extensions=["tables", "sane_lists"])
        except ImportError:
            body = "<pre>" + html.escape(md_text) + "</pre>"
        title = html.escape(self.r.passport.document_title or self.r.passport.document_name)
        css = """
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:1100px;margin:32px auto;padding:0 24px;
color:#1d1d1f;line-height:1.55;font-size:15px}
h1{font-size:26px;border-bottom:3px solid #0a7d55;padding-bottom:8px}h2{font-size:21px;margin-top:36px;
border-bottom:1px solid #ddd;padding-bottom:4px;color:#0a5c40}h3{font-size:17px;margin-top:24px}h4{font-size:15px;
margin:18px 0 6px;padding:6px 10px;background:#f3f6f5;border-left:4px solid #0a7d55}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}th,td{border:1px solid #d5d9d8;padding:5px 7px;
vertical-align:top}th{background:#eef3f1}blockquote{margin:6px 0 6px 0;padding:4px 12px;border-left:3px solid #c2cbc8;
color:#333;background:#fafbfa}code{background:#f0f0f0;padding:1px 4px;border-radius:3px}
@media print{body{margin:0;max-width:none;font-size:12px}h2{page-break-after:avoid}h4{page-break-after:avoid}}
"""
        return (f"<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\"><title>Отчёт: {title}</title>"
                f"<style>{css}</style></head><body>{body}</body></html>")

    def answers_template(self) -> str:
        lines = ["# Ответы автора ТЗ на уточняющие вопросы агента.",
                 "# Впишите ответ в кавычках после номера вопроса и запустите:",
                 "#   python -m tz_auditor <документ> <отчёт> --answers <этот файл>", ""]
        for q in self.r.questions:
            lines.append(f"# {q.qid} ({PRIO_RU[q.priority]}): {q.question}")
            for o in q.options:
                lines.append(f"#   {o}")
            lines.append(f'{q.qid}: ""')
            lines.append("")
        return "\n".join(lines)


def write_reports(result: AuditResult, doc: Document, out_path: str) -> list[Path]:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    w = ReportWriter(result, doc)
    md_text = w.markdown()
    stem = out.with_suffix("")
    written = []
    if out.suffix.lower() in (".html", ".htm"):
        out.write_text(w.html(md_text), "utf-8")
        written.append(out)
        md_path = stem.with_suffix(".md")
    else:
        md_path = out if out.suffix else out.with_suffix(".md")
    md_path.write_text(md_text, "utf-8")
    written.append(md_path)
    html_path = stem.with_suffix(".html")
    if html_path not in written:
        html_path.write_text(w.html(md_text), "utf-8")
        written.append(html_path)
    json_path = stem.with_suffix(".json")
    json_path.write_text(json.dumps(result.model_dump(), ensure_ascii=False, indent=1), "utf-8")
    written.append(json_path)
    if result.questions:
        ans = Path(str(stem) + ".answers.yaml")
        if not ans.exists():
            ans.write_text(w.answers_template(), "utf-8")
            written.append(ans)
    return written
