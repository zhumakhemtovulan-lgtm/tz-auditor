"""Конвейер проверки ТЗ.

Шаги:
 1. Чтение документа (loader)            — без ИИ
 2. Автоматические правила (rules)       — без ИИ
 3. Нарезка на требования                — ИИ + проверка цитат кодом
 4. Проверка каждого требования          — ИИ, пакетами
 5. Проверка набора + сценарный прогон   — ИИ
 6. Верификация цитат                    — без ИИ
 7. Независимый критик                   — ИИ (второй аудитор)
 8. Вопросы, чек-лист, итог              — ИИ + правило итогового решения
 9. Уточнение (по желанию)               — ответы человека → ИИ пересматривает выводы
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from . import prompts as P
from .config import Settings
from .llm import LLMClient, LLMError
from .loader import Document, load_document
from .rules import run_rules
from .schemas import (SEVERITY_ORDER, AuditResult, ChecklistItem, CriterionScore, Finding,
                      Question, Requirement, RequirementAssessment, RunPassport, Scenario, Verdict)
from .verify import QuoteIndex, clean_line

BATCH_SIZE = 8
MAX_CONTEXT_LINES = 1500
ISO_INDIVIDUAL = ["NECESSARY", "APPROPRIATE", "UNAMBIGUOUS", "COMPLETE", "SINGULAR",
                  "FEASIBLE", "VERIFIABLE", "CORRECT", "CONFORMING"]
SET_CODES = ["SET_COMPLETE", "SET_CONSISTENT", "SET_FEASIBLE", "SET_COMPREHENSIBLE",
             "SET_VALIDATABLE", "RANKED", "MODIFIABLE", "TRACEABLE"]


def agent_version() -> str:
    root = Path(__file__).resolve().parent.parent
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                                capture_output=True, text=True, timeout=5).stdout.strip()
        if commit:
            dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                                   capture_output=True, text=True, timeout=5).stdout.strip()
            tag = subprocess.run(["git", "describe", "--tags", "--exact-match"], cwd=root,
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            v = f"{__version__} (коммит {commit}"
            v += f", тег {tag}" if tag else ""
            v += ", есть незакоммиченные изменения)" if dirty else ")"
            return v
    except Exception:  # noqa: BLE001
        pass
    return f"{__version__} (вне git)"


def _need(data, key, typ=list):
    if not isinstance(data, dict) or key not in data or not isinstance(data[key], typ):
        raise ValueError(f"в ответе нет поля «{key}» нужного типа")


def _dump(items, fields) -> str:
    rows = []
    for it in items:
        d = it.model_dump() if hasattr(it, "model_dump") else it
        rows.append({k: d.get(k) for k in fields if d.get(k) not in (None, "", [])})
    return json.dumps(rows, ensure_ascii=False, indent=1)


class Auditor:
    def __init__(self, settings: Settings, log=print):
        self.s = settings
        self.log = log
        self.llm = LLMClient(settings, log=log)
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ helpers
    def _doc_context(self, doc: Document) -> str:
        if len(doc.lines) <= MAX_CONTEXT_LINES:
            return doc.numbered()
        return doc.numbered(1, MAX_CONTEXT_LINES) + "\n[... документ обрезан для контекста ...]"

    def _signals_text(self, signals: list[Finding]) -> str:
        if not signals:
            return "нет"
        return "\n".join(f"- {f.title}: строки {f.lines[:10]}" for f in signals)

    def _parse_findings(self, raw: list, scope: str) -> list[Finding]:
        out = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["scope"] = scope
            if isinstance(item.get("quotes"), str):
                item["quotes"] = [item["quotes"]]
            if item.get("severity") not in ("critical", "major", "minor"):
                item["severity"] = "major"
            for k in ("title", "description"):
                item.setdefault(k, "")
            item.setdefault("criterion", "")
            try:
                out.append(Finding.model_validate({k: v for k, v in item.items() if k in Finding.model_fields}))
            except Exception as exc:  # noqa: BLE001
                self.log(f"  ! пропущено замечание не по схеме: {exc}")
        return out

    # ------------------------------------------------------------------ steps
    def extract(self, doc: Document, index: QuoteIndex) -> tuple[list[Requirement], str]:
        chunks = []
        if len(doc.lines) <= MAX_CONTEXT_LINES:
            chunks.append(doc.numbered())
        else:
            for start in range(1, len(doc.lines) + 1, MAX_CONTEXT_LINES):
                chunks.append(doc.numbered(start, min(len(doc.lines), start + MAX_CONTEXT_LINES - 1)))
        reqs: list[Requirement] = []
        title = ""
        for n, chunk in enumerate(chunks, 1):
            data = self.llm.complete_json(
                f"extract-{n}", P.SYSTEM, P.fill(P.EXTRACT, document=chunk),
                validate=lambda d: _need(d, "requirements"),
            )
            title = title or str(data.get("document_title", ""))
            for item in data["requirements"]:
                if not isinstance(item, dict) or not item.get("quote"):
                    continue
                lines = [int(x) for x in item.get("lines", []) if str(x).isdigit()]
                r = Requirement(
                    rid="", doc_id=str(item.get("doc_id", "")), section=str(item.get("section", "")),
                    kind=str(item.get("kind", "")), summary=str(item.get("summary", "")),
                    quote=str(item["quote"]), lines=lines,
                )
                ok, found_lines, note = index.find(r.quote)
                if ok:
                    r.quote_verified = True
                    r.lines = found_lines or r.lines
                    if "приблизительное" in note:
                        r.quote = " ".join(clean_line(doc.lines[i - 1]) for i in r.lines)[:400]
                elif r.lines and all(1 <= i <= len(doc.lines) for i in r.lines):
                    r.quote = " ".join(clean_line(doc.lines[i - 1]) for i in r.lines)[:400]
                    r.quote_verified = True
                else:
                    self.log(f"  ! требование без подтверждённой цитаты отброшено: {r.summary[:60]}")
                    continue
                reqs.append(r)
        seen, uniq = set(), []
        for r in sorted(reqs, key=lambda x: (x.lines[0] if x.lines else 10**6)):
            key = (r.doc_id, tuple(r.lines))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        for i, r in enumerate(uniq, 1):
            r.rid = f"R-{i:02d}"
        return uniq, title

    def assess(self, doc: Document, reqs: list[Requirement], signals: list[Finding]):
        assessments: list[RequirementAssessment] = []
        findings: list[Finding] = []
        ctx = self._doc_context(doc)
        common = dict(criteria=P.methodology("criteria.md"), domain=P.methodology("domain_rules.md"),
                      severity=P.methodology("severity.md"), signals=self._signals_text(signals))
        for b in range(0, len(reqs), BATCH_SIZE):
            batch = reqs[b:b + BATCH_SIZE]
            batch_txt = _dump(batch, ["rid", "doc_id", "section", "kind", "quote", "lines"])
            data = self.llm.complete_json(
                f"assess-{b // BATCH_SIZE + 1}", P.SYSTEM,
                P.fill(P.ASSESS, batch=batch_txt, document=ctx, **common),
                validate=lambda d: (_need(d, "assessments"), _need(d, "findings")),
            )
            rids = {r.rid for r in batch}
            for a in data.get("assessments", []):
                if not isinstance(a, dict) or a.get("rid") not in rids:
                    continue
                scores = []
                for sc in a.get("scores", []):
                    if isinstance(sc, dict) and sc.get("criterion") in ISO_INDIVIDUAL:
                        st = sc.get("status") if sc.get("status") in ("ok", "issue", "n/a") else "ok"
                        scores.append(CriterionScore(criterion=sc["criterion"], status=st,
                                                     comment=str(sc.get("comment", ""))))
                v = a.get("verdict") if a.get("verdict") in ("ok", "needs_fix", "blocker") else "needs_fix"
                assessments.append(RequirementAssessment(rid=a["rid"], scores=scores, verdict=v))
            for f in self._parse_findings(data.get("findings"), "requirement"):
                f.requirement_ids = [x for x in f.requirement_ids if x in rids] or f.requirement_ids
                findings.append(f)
        have = {a.rid for a in assessments}
        for r in reqs:
            if r.rid not in have:
                assessments.append(RequirementAssessment(rid=r.rid, scores=[], verdict="ok"))
        return assessments, findings

    def set_review(self, doc, reqs, known, signals):
        data = self.llm.complete_json(
            "set-review", P.SYSTEM,
            P.fill(P.SET_REVIEW, criteria=P.methodology("criteria.md"), domain=P.methodology("domain_rules.md"),
                   severity=P.methodology("severity.md"),
                   requirements=_dump(reqs, ["rid", "doc_id", "kind", "summary", "lines"]),
                   known=_dump(known, ["requirement_ids", "criterion", "title"]),
                   signals=self._signals_text(signals), document=self._doc_context(doc)),
            validate=lambda d: (_need(d, "findings"), _need(d, "set_scores")),
        )
        scores = []
        for sc in data.get("set_scores", []):
            if isinstance(sc, dict) and sc.get("criterion"):
                st = sc.get("status") if sc.get("status") in ("ok", "issue", "n/a") else "issue"
                scores.append(CriterionScore(criterion=str(sc["criterion"]), status=st,
                                             comment=str(sc.get("comment", ""))))
        scenarios = []
        for sc in data.get("scenarios", []) or []:
            try:
                if sc.get("status") not in ("ok", "conflict", "gap"):
                    sc["status"] = "gap"
                scenarios.append(Scenario.model_validate(sc))
            except Exception:  # noqa: BLE001
                continue
        return (str(data.get("set_summary", "")), scores,
                self._parse_findings(data.get("findings"), "set"), scenarios)

    def verify(self, doc: Document, index: QuoteIndex, findings: list[Finding]):
        """Каждая цитата ищется в документе. Приблизительные заменяются точным текстом документа."""
        kept, rejected = [], []
        for f in findings:
            if not f.quotes:
                f.verified, f.verification_note = False, "нет цитаты из документа"
                rejected.append(f)
                continue
            new_quotes, all_lines, notes, ok_all = [], [], [], True
            for q in f.quotes:
                ok, ql, note = index.find(q)
                ql = [i for i in ql if doc.lines[i - 1].strip()]
                if not ok:
                    ok_all = False
                    notes.append(f"«{q[:60]}»: {note}")
                    new_quotes.append(q)
                    continue
                if "приблизительное" in note and ql:
                    q = " ".join(clean_line(doc.lines[i - 1]) for i in ql)[:300]
                    notes.append("одна цитата уточнена по точному тексту документа")
                new_quotes.append(q)
                all_lines.extend(ql)
            f.quotes = new_quotes
            f.lines = sorted(set(all_lines)) or f.lines
            if ok_all:
                f.verified = True
                f.verification_note = "найдены в документе" + (f" ({'; '.join(sorted(set(notes)))})" if notes else "")
                kept.append(f)
            else:
                f.verified = False
                f.verification_note = "; ".join(notes)
                rejected.append(f)
        return kept, rejected

    def critic(self, doc, findings: list[Finding]):
        if not findings:
            return findings, []
        for i, f in enumerate(findings, 1):
            f.fid = f"P-{i:02d}"
        data = self.llm.complete_json(
            "critic", P.SYSTEM,
            P.fill(P.CRITIC, severity=P.methodology("severity.md"),
                   findings=_dump(findings, ["fid", "requirement_ids", "criterion", "severity", "title",
                                             "description", "quotes"]),
                   document=self._doc_context(doc)),
            validate=lambda d: _need(d, "reviews"),
        )
        by_id = {f.fid: f for f in findings}
        for rv in data.get("reviews", []):
            if not isinstance(rv, dict) or rv.get("fid") not in by_id:
                continue
            f = by_id[rv["fid"]]
            dup = rv.get("duplicate_of") or ""
            verdict = rv.get("verdict")
            if dup and dup in by_id and dup != f.fid:
                verdict = "rejected"
                rv["note"] = f"дубль замечания «{by_id[dup].title}». " + str(rv.get("note", ""))
                main = by_id[dup]
                main.requirement_ids = sorted(set(main.requirement_ids) | set(f.requirement_ids))
            f.critic_verdict = "confirmed" if verdict == "confirmed" else "rejected" if verdict == "rejected" else "unchecked"
            f.critic_note = str(rv.get("note", ""))
            if rv.get("severity") in ("critical", "major", "minor"):
                f.severity = rv["severity"]
        kept = [f for f in findings if f.critic_verdict != "rejected"]
        rejected = [f for f in findings if f.critic_verdict == "rejected"]
        return kept, rejected

    def synthesize(self, doc, reqs, findings):
        data = self.llm.complete_json(
            "synthesis", P.SYSTEM,
            P.fill(P.SYNTH, questions_method=P.methodology("questions.md"), severity=P.methodology("severity.md"),
                   checklist=P.methodology("checklist.md"),
                   requirements=_dump(reqs, ["rid", "doc_id", "summary"]),
                   findings=_dump(findings, ["fid", "requirement_ids", "severity", "title", "description", "quotes"]),
                   document=self._doc_context(doc)),
            validate=lambda d: (_need(d, "questions"), _need(d, "checklist"), _need(d, "verdict", dict)),
        )
        questions = []
        for q in data.get("questions", []):
            try:
                if isinstance(q.get("options"), str):
                    q["options"] = [q["options"]]
                if q.get("priority") not in ("high", "medium", "low"):
                    q["priority"] = "medium"
                questions.append(Question.model_validate({k: v for k, v in q.items() if k in Question.model_fields}))
            except Exception:  # noqa: BLE001
                continue
        order = {"high": 0, "medium": 1, "low": 2}
        questions.sort(key=lambda q: order[q.priority])
        for i, q in enumerate(questions, 1):
            q.qid = f"Q-{i:02d}"
        checklist = []
        allowed = ("Да", "Нет", "Частично", "Не требуется", "Нет данных")
        for c in data.get("checklist", []):
            if isinstance(c, dict) and c.get("item"):
                if c.get("status") not in allowed:
                    c["status"] = "Нет данных"
                checklist.append(ChecklistItem.model_validate({k: v for k, v in c.items() if k in ChecklistItem.model_fields}))
        v = data.get("verdict", {})
        verdict = Verdict(
            ready_for_development=v.get("ready_for_development") if v.get("ready_for_development") in ("да", "нет", "условно") else "нет",
            ready_for_testing=v.get("ready_for_testing") if v.get("ready_for_testing") in ("да", "нет", "условно") else "нет",
            summary=str(v.get("summary", "")),
            top_fixes=[str(x) for x in v.get("top_fixes", [])],
        )
        # Итоговое решение по правилу шкалы критичности (детерминированно, без ИИ).
        sev = {f.severity for f in findings}
        rule_verdict = "нет" if "critical" in sev else "условно" if "major" in sev else "да"
        if verdict.ready_for_development != rule_verdict:
            verdict.ready_for_development = rule_verdict
        if rule_verdict == "нет" and verdict.ready_for_testing == "да":
            verdict.ready_for_testing = "нет"
        return questions, checklist, verdict

    def clarify(self, doc, findings, questions, answers: dict[str, str]):
        qa = []
        for q in questions:
            a = answers.get(q.qid, "").strip()
            q.answer = a
            if a:
                qa.append({"qid": q.qid, "question": q.question, "answer": a, "finding_ids": q.finding_ids})
        if not qa:
            return []
        data = self.llm.complete_json(
            "clarify", P.SYSTEM,
            P.fill(P.CLARIFY, qa=json.dumps(qa, ensure_ascii=False, indent=1),
                   findings=_dump(findings, ["fid", "title", "description", "quotes"]),
                   document=self._doc_context(doc)),
            validate=lambda d: _need(d, "log"),
        )
        return [x for x in data.get("log", []) if isinstance(x, dict)]

    # ------------------------------------------------------------------ main
    def run(self, doc_path: str, answers: dict[str, str] | None = None, ask=None) -> tuple[AuditResult, Document]:
        t0 = time.time()
        started = datetime.now().astimezone()
        self.log("1/8 Чтение документа")
        doc = load_document(doc_path)
        for w in doc.warnings:
            self.log(f"  ! {w}")
        index = QuoteIndex(doc)

        self.log("2/8 Автоматические правила (без ИИ)")
        signals = run_rules(doc)
        self.log(f"  · сигналов: {len(signals)}")

        self.log("3/8 Нарезка на требования")
        reqs, title = self.extract(doc, index)
        self.log(f"  · требований: {len(reqs)}")

        self.log("4/8 Проверка каждого требования")
        assessments, req_findings = self.assess(doc, reqs, signals)

        self.log("5/8 Проверка набора и сценарный прогон")
        try:
            set_summary, set_scores, set_findings, scenarios = self.set_review(doc, reqs, req_findings, signals)
        except LLMError as exc:
            self.warnings.append(f"Шаг «проверка набора» не выполнен: {exc}.")
            self.log(f"  ! {self.warnings[-1]}")
            set_summary, set_scores, set_findings, scenarios = "", [], [], []

        self.log("6/8 Верификация цитат (без ИИ)")
        findings, rejected = self.verify(doc, index, req_findings + set_findings)
        for f in rejected:
            f.critic_note = "отклонено верификатором: " + f.verification_note
        self.log(f"  · подтверждено цитатами: {len(findings)}, отклонено: {len(rejected)}")

        self.log("7/8 Независимый критик")
        try:
            findings, critic_rejected = self.critic(doc, findings)
        except LLMError as exc:
            self.warnings.append(f"Шаг «критик» не выполнен: {exc}. Замечания не прошли вторую проверку.")
            self.log(f"  ! {self.warnings[-1]}")
            critic_rejected = []
        rejected += critic_rejected
        self.log(f"  · осталось замечаний: {len(findings)}, отклонено критиком: {len(critic_rejected)}")

        findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.lines[0] if f.lines else 10**6))
        for i, f in enumerate(findings, 1):
            f.fid = f"F-{i:02d}"
        for i, f in enumerate(rejected, 1):
            f.fid = f"X-{i:02d}"

        sev_by_rid: dict[str, set] = {}
        for f in findings:
            for rid in f.requirement_ids:
                sev_by_rid.setdefault(rid, set()).add(f.severity)
        for a in assessments:
            s = sev_by_rid.get(a.rid, set())
            a.verdict = "blocker" if "critical" in s else "needs_fix" if s else "ok"

        self.log("8/8 Вопросы, чек-лист и итог")
        try:
            questions, checklist, verdict = self.synthesize(doc, reqs, findings)
        except LLMError as exc:
            self.warnings.append(f"Шаг «вопросы и итог» не выполнен: {exc}.")
            self.log(f"  ! {self.warnings[-1]}")
            sev = {f.severity for f in findings}
            rv = "нет" if "critical" in sev else "условно" if "major" in sev else "да"
            questions, checklist = [], []
            verdict = Verdict(ready_for_development=rv, ready_for_testing=rv,
                              summary="Итог определён по шкале критичности без шага синтеза.")

        clar_log = []
        if ask is not None and questions:
            answers = dict(answers or {})
            answers.update(ask(questions))
        if answers:
            self.log("+ Уточнение: пересмотр выводов по ответам")
            try:
                clar_log = self.clarify(doc, findings, questions, answers)
            except LLMError as exc:
                self.warnings.append(f"Шаг «уточнение» не выполнен: {exc}.")

        finished = datetime.now().astimezone()
        passport = RunPassport(
            document_name=doc.name, document_title=title or doc.title, document_path=str(doc.path),
            document_sha256=doc.sha256, document_format=doc.fmt.upper(),
            run_started=started.strftime("%Y-%m-%d %H:%M:%S %Z"),
            run_finished=finished.strftime("%Y-%m-%d %H:%M:%S %Z"),
            duration_sec=round(time.time() - t0, 1), provider=self.s.provider, model=self.llm.model_label(),
            agent_version=agent_version(), llm_calls=self.llm.usage.calls,
            tokens_in=self.llm.usage.tokens_in, tokens_out=self.llm.usage.tokens_out,
            cache_hits=self.llm.usage.cache_hits, warnings=doc.warnings + self.warnings,
        )
        result = AuditResult(
            passport=passport, requirements=reqs, assessments=sorted(assessments, key=lambda a: a.rid),
            findings=findings, rejected_findings=rejected, rule_signals=signals,
            set_summary=set_summary, set_scores=set_scores, scenarios=scenarios,
            questions=questions, checklist=checklist, clarification_log=clar_log, verdict=verdict,
        )
        return result, doc
