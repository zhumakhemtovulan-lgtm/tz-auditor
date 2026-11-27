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
from .schemas import (SEVERITY_ORDER, AcceptanceCheck, AuditResult, RuleCheck, ChecklistItem, CriterionScore, Finding,
                      Question, Requirement, RequirementAssessment, RunPassport, Scenario, Verdict)
from .verify import QuoteIndex, clean_line

BATCH_SIZE = 8
MAX_CONTEXT_LINES = 1500
ISO_INDIVIDUAL = ["NECESSARY", "APPROPRIATE", "UNAMBIGUOUS", "COMPLETE", "SINGULAR",
                  "FEASIBLE", "VERIFIABLE", "CORRECT", "CONFORMING"]
SET_CODES = ["SET_COMPLETE", "SET_CONSISTENT", "SET_FEASIBLE", "SET_COMPREHENSIBLE",
             "SET_VALIDATABLE", "SET_NO_DUPLICATES", "SET_NO_DESIGN", "SET_NO_TBD",
             "RANKED", "MODIFIABLE", "TRACEABLE"]


def agent_version() -> str:
    root = Path(__file__).resolve().parent.parent
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                                capture_output=True, text=True, timeout=5).stdout.strip()
        if commit:
            dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", ".",
                                    ":(exclude)reports"], cwd=root,
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
        self.ac_all_match: set[str] = set()
        self.passes = 1
        self._tag = ""
        self._variant = ""

    def _call(self, step: str, system: str, user: str, validate=None):
        """Вызов модели с учётом номера прохода (для двойной проверки)."""
        return self.llm.complete_json(step + self._tag, system, user + self._variant, validate=validate)

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
            data = self._call(
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

    def ac_check(self, doc, reqs: list[Requirement]):
        """Критерии приёмки «исполняются» по правилам документа, результат сравнивается с ожидаемым."""
        acs = [r for r in reqs if "приём" in r.kind.lower() or "прием" in r.kind.lower()]
        frs = [r for r in reqs if r.kind.lower().startswith("функц")]
        if not acs:
            f = Finding(scope="set", criterion="SET_VALIDATABLE", rule="DOC-TRACE", severity="critical",
                        title="В документе нет критериев приёмки",
                        description="Не найдено ни одного критерия приёмки: требования нечем проверить.",
                        quotes=[frs[0].quote[:200]] if frs else [], requirement_ids=[r.rid for r in frs])
            return [], [r.rid for r in frs], [f] if frs else []
        prompt = P.fill(P.AC_CHECK, requirements=_dump(reqs, ["rid", "doc_id", "kind", "quote"]),
                        criteria_list=_dump(acs, ["rid", "doc_id", "quote"]), document=self._doc_context(doc))
        # Два независимых прохода: слабая модель иногда путает даты и роли чисел.
        # Если хотя бы один проход нашёл расхождение, оно идёт к критику с обоими выводами.
        runs = []
        for n, variant in enumerate(("", "\n\nПроверяй критерии в обратном порядке и особенно внимательно "
                                         "к датам: дата совершения и дата проведения — разные факты."), 1):
            try:
                runs.append(self.llm.complete_json(f"ac-check-{n}", P.SYSTEM, prompt + variant,
                                                   validate=lambda d: _need(d, "checks")))
            except LLMError as exc:
                if not runs and n == 2:
                    raise
                self.log(f"  ! ac-check-{n}: {exc}")
        data = {"checks": [], "coverage": []}
        merged: dict[str, dict] = {}
        self.ac_all_match = set()
        verdicts: dict[str, list[str]] = {}
        for run in runs:
            for c in run.get("checks", []):
                if isinstance(c, dict):
                    verdicts.setdefault(c.get("criterion_rid"), []).append(c.get("verdict"))
        self.ac_all_match = {k for k, v in verdicts.items() if len(v) >= 2 and all(x == "match" for x in v)}
        rank = {"mismatch": 0, "undetermined": 1, "match": 2}
        for run in runs:
            for c in run.get("checks", []):
                if not isinstance(c, dict):
                    continue
                cur = merged.get(c.get("criterion_rid"))
                if cur is None or rank.get(c.get("verdict"), 1) < rank.get(cur.get("verdict"), 1):
                    merged[c.get("criterion_rid")] = c
            data["coverage"].extend(run.get("coverage", []) or [])
        data["checks"] = list(merged.values())
        by_id = {r.rid: r for r in reqs}
        checks, findings = [], []
        for c in data.get("checks", []):
            if not isinstance(c, dict) or c.get("criterion_rid") not in by_id:
                continue
            if c.get("verdict") not in ("match", "mismatch", "undetermined"):
                c["verdict"] = "undetermined"
            ac = AcceptanceCheck.model_validate({k: v for k, v in c.items() if k in AcceptanceCheck.model_fields})
            checks.append(ac)
            if ac.verdict == "match":
                continue
            crit_req = by_id[ac.criterion_rid]
            rule_rids = [st.get("rid") for st in ac.steps if isinstance(st, dict) and st.get("rid") in by_id]
            quotes = [q for q in c.get("quotes", []) if isinstance(q, str)] or [crit_req.quote[:200]]
            if ac.verdict == "mismatch":
                findings.append(Finding(
                    scope="set", criterion="SET_CONSISTENT", rule="DOC-TRACE", severity="critical",
                    requirement_ids=[ac.criterion_rid] + rule_rids,
                    title=f"Критерий приёмки {crit_req.doc_id} противоречит требованиям",
                    description=(f"По требованиям получается: {ac.derived_result}. В критерии ожидается: "
                                 f"{ac.stated_result}. {ac.explanation}"),
                    quotes=quotes,
                    impact="Разработчик реализует правило из требования, а тестировщик проверит по критерию: "
                           "тест упадёт или будет принят неверный результат.",
                    protected=True,
                    recommendation="Решить, какое правило верное, и привести требование и критерий к одному результату."))
            else:
                findings.append(Finding(
                    scope="set", criterion="SET_COMPLETE", rule="DOC-TRACE", severity="major",
                    requirement_ids=[ac.criterion_rid] + rule_rids,
                    title=f"Результат критерия {crit_req.doc_id} не выводится из требований",
                    description=f"{ac.explanation} Ожидается: {ac.stated_result}.",
                    quotes=quotes,
                    recommendation="Дописать в требования правило, из которого следует ожидаемый результат."))
        covered = set()
        for cv in data.get("coverage", []) or []:
            if isinstance(cv, dict) and cv.get("covered_by"):
                covered.add(cv.get("rid"))
        uncovered = [r.rid for r in frs if r.rid not in covered]
        if uncovered:
            ur = [by_id[x] for x in uncovered]
            findings.append(Finding(
                scope="set", criterion="TRACEABLE", rule="DOC-TRACE", severity="major",
                requirement_ids=uncovered,
                title="Функциональные требования без критериев приёмки",
                description="Для требований " + ", ".join(r.doc_id or r.rid for r in ur) +
                            " нет ни одного критерия приёмки: их выполнение нечем подтвердить.",
                quotes=[r.quote[:150] for r in ur[:3]],
                recommendation="Добавить критерии приёмки (условия, действие, ожидаемый результат) для каждого требования."))
        return checks, uncovered, findings

    def rules_pass(self, doc, reqs: list[Requirement]):
        """Явный проход по предметным правилам методики команды."""
        data = self._call(
            "rules-pass", P.SYSTEM,
            P.fill(P.RULES_PASS, domain=P.methodology("domain_rules.md"), severity=P.methodology("severity.md"),
                   requirements=_dump(reqs, ["rid", "doc_id", "kind", "summary"]), document=self._doc_context(doc)),
            validate=lambda d: _need(d, "rules"),
        )
        checks, findings = [], []
        for rc in data.get("rules", []):
            if not isinstance(rc, dict) or not rc.get("code"):
                continue
            if rc.get("status") not in ("ok", "violated", "missing", "n/a"):
                rc["status"] = "n/a"
            checks.append(RuleCheck.model_validate({k: v for k, v in rc.items() if k in RuleCheck.model_fields}))
            if rc["status"] in ("violated", "missing"):
                sev = rc.get("severity") if rc.get("severity") in ("critical", "major", "minor") else "major"
                quotes = rc.get("quotes") or []
                if isinstance(quotes, str):
                    quotes = [quotes]
                findings.append(Finding(
                    scope="set", criterion="SET_CONSISTENT" if rc["status"] == "violated" else "SET_COMPLETE",
                    rule=str(rc["code"]), severity=sev, requirement_ids=list(rc.get("requirement_ids") or []),
                    title=(f"Нарушено правило {rc['code']}" if rc["status"] == "violated" else f"Не описано: {rc['code']}")
                          + (f" — {str(rc.get('scenario'))[:80]}" if rc.get("scenario") else ""),
                    description=str(rc.get("explanation", "")) + (f" Сценарий: {rc['scenario']}" if rc.get("scenario") else ""),
                    quotes=[str(q) for q in quotes],
                    recommendation="Дописать правило в требования и добавить критерий приёмки для этого сценария."))
        return checks, findings

    def pair_check(self, doc, reqs: list[Requirement]) -> list[Finding]:
        """Попарно сравнивает функциональные требования: ищет ситуацию, где они дают несовместимый результат."""
        frs = [r for r in reqs if r.kind.lower().startswith("функц")][:10]
        if len(frs) < 2:
            return []
        pairs = [{"a": a.rid, "b": b.rid} for i, a in enumerate(frs) for b in frs[i + 1:]]
        data = self._call(
            "pair-check", P.SYSTEM,
            P.fill(P.PAIR_CHECK, requirements=_dump(frs, ["rid", "doc_id", "quote"]),
                   pairs=json.dumps(pairs, ensure_ascii=False), document=self._doc_context(doc)),
            validate=lambda d: _need(d, "pairs"),
        )
        by_id = {r.rid: r for r in frs}
        out = []
        for p in data.get("pairs", []):
            if not isinstance(p, dict) or p.get("conflict") is not True:
                continue
            a, b = by_id.get(p.get("a")), by_id.get(p.get("b"))
            if not a or not b:
                continue
            quotes = [q for q in (p.get("quotes") or []) if isinstance(q, str)] or [a.quote[:200], b.quote[:200]]
            out.append(Finding(
                scope="set", criterion="SET_CONSISTENT", rule="FIN-CONFLICT", severity="critical",
                requirement_ids=[a.rid, b.rid],
                title=f"Требования {a.doc_id or a.rid} и {b.doc_id or b.rid} дают несовместимый результат",
                description=(f"Ситуация: {p.get('situation', '')}. По {a.doc_id or a.rid}: {p.get('result_a', '')}. "
                             f"По {b.doc_id or b.rid}: {p.get('result_b', '')}. {p.get('explanation', '')}"),
                quotes=quotes,
                impact="Разработчик выберет одно из правил наугад; тестировщик проверит по другому.",
                recommendation="Определить приоритет правил для этой ситуации и записать его явно, добавить критерий приёмки."))
        return out

    def _scenario_findings(self, scenarios, reqs: list[Requirement]) -> list[Finding]:
        """Конфликт или пробел в сценарном прогоне — это замечание. Цитаты берём из задействованных требований."""
        by_id = {r.rid: r for r in reqs}
        out = []
        for sc in scenarios:
            if sc.status == "ok":
                continue
            rids = [x for x in sc.rules_applied if x in by_id]
            rids += [x.get("rid") for x in sc.per_rule if isinstance(x, dict) and x.get("rid") in by_id and x.get("rid") not in rids]
            if not rids:
                continue
            per = "; ".join(f"{x.get('rid')}: {x.get('result')}" for x in sc.per_rule if isinstance(x, dict))
            conflict = sc.status == "conflict"
            out.append(Finding(
                scope="set", criterion="SET_CONSISTENT" if conflict else "SET_COMPLETE",
                rule="FIN-CONFLICT" if conflict else "", severity="critical" if conflict else "major",
                requirement_ids=rids,
                title=("Сценарий даёт разный результат по разным пунктам: " if conflict else "Сценарий не описан: ")
                      + sc.scenario[:90],
                description=f"Сценарий: {sc.scenario}. Итог по документу: {sc.outcome}." + (f" По пунктам: {per}." if per else ""),
                quotes=[by_id[x].quote[:220] for x in rids[:3]],
                impact="Разработчик и тестировщик по-разному реализуют и проверят этот случай." if conflict
                       else "Поведение системы в этом случае будет выбрано разработчиком наугад.",
                recommendation="Описать результат для этого сценария одним правилом и добавить критерий приёмки."))
        return out

    def set_review(self, doc, reqs, known, signals):
        data = self._call(
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

    def critic(self, doc, findings: list[Finding], evidence: str = "нет"):
        if not findings:
            return findings, []
        for i, f in enumerate(findings, 1):
            f.fid = f"P-{i:02d}"
        dump = _dump(findings, ["fid", "source", "requirement_ids", "criterion", "rule", "severity",
                                "title", "description", "quotes"])
        runs = []
        # Два независимых прохода критика (второй — в обратном порядке замечаний).
        for n, items in enumerate((dump, json.dumps(list(reversed(json.loads(dump))), ensure_ascii=False, indent=1)), 1):
            try:
                runs.append(self.llm.complete_json(
                    f"critic-{n}", P.SYSTEM,
                    P.fill(P.CRITIC, severity=P.methodology("severity.md"), evidence=evidence,
                           findings=items, document=self._doc_context(doc)),
                    validate=lambda d: _need(d, "reviews")))
            except LLMError as exc:
                if n == 2 and runs:
                    self.log(f"  ! critic-2: {exc}")
                    continue
                raise
        votes: dict[str, list[dict]] = {}
        for run in runs:
            for rv in run.get("reviews", []):
                if isinstance(rv, dict) and rv.get("fid"):
                    votes.setdefault(rv["fid"], []).append(rv)
        reviews = []
        for fid, vs in votes.items():
            first = dict(vs[0])
            false_votes = [v for v in vs if v.get("verdict") == "rejected" and not v.get("duplicate_of")]
            dup_votes = [v for v in vs if v.get("duplicate_of")]
            if dup_votes:
                first["duplicate_of"] = dup_votes[0]["duplicate_of"]
                first["verdict"] = "rejected"
                first["note"] = dup_votes[0].get("note", "")
            elif len(false_votes) == len(vs) and len(vs) == len(runs):
                first["verdict"] = "rejected"
                first["note"] = false_votes[0].get("note", "")
            else:
                first["verdict"] = "confirmed"
                kept_note = [v for v in vs if v.get("verdict") == "confirmed"]
                first["note"] = (kept_note[0].get("note", "") if kept_note else "")
                if false_votes:
                    first["note"] = ("один из двух проходов критика сомневался: " + str(false_votes[0].get("note", ""))[:150]
                                     + (" | " + first["note"] if first["note"] else ""))
            reviews.append(first)
        data = {"reviews": reviews}
        by_id = {f.fid: f for f in findings}
        dup_of: dict[str, str] = {}
        for rv in data.get("reviews", []):
            if not isinstance(rv, dict) or rv.get("fid") not in by_id:
                continue
            f = by_id[rv["fid"]]
            dup = rv.get("duplicate_of") or ""
            verdict = rv.get("verdict")
            if dup and dup in by_id and dup != f.fid:
                dup_of[f.fid] = dup
            f.critic_verdict = "confirmed" if verdict == "confirmed" else "rejected" if verdict == "rejected" else "unchecked"
            f.critic_note = str(rv.get("note", ""))
            if f.protected and f.critic_verdict == "rejected" and not dup:
                f.critic_verdict = "confirmed"
                f.critic_note = ("замечание подтверждено жёстким доказательством (правило без ИИ или пересчёт "
                                 "критерия приёмки); сомнение критика: " + f.critic_note[:150])
            if rv.get("severity") in ("critical", "major", "minor"):
                f.severity = rv["severity"]
        # Дубли: идём по цепочке до «главного» замечания. Взаимные дубли (A→B, B→A) не должны
        # удалить оба: в цикле главным становится первое по порядку.
        def head(fid: str) -> str:
            seen, cur = [fid], dup_of[fid]
            while cur in dup_of and cur not in seen:
                seen.append(cur)
                cur = dup_of[cur]
            return min(seen) if cur in seen else cur

        heads = {fid: head(fid) for fid in dup_of}
        # Если в группе дублей есть защищённое замечание, а главным выбрано незащищённое,
        # защищённое переносит свойства на главное (главное наследует защиту).
        for fid, h in heads.items():
            if by_id[fid].protected and not by_id[h].protected:
                by_id[h].protected = True
                if by_id[h].critic_verdict == "rejected" and h not in dup_of:
                    by_id[h].critic_verdict = "confirmed"
        for h in set(heads.values()):
            main = by_id[h]
            if h in dup_of:  # главное в цикле дублей: отклонено было только как дубль
                main.critic_verdict = "confirmed"
                main.critic_note = "основное замечание в группе дублей"
        for fid, h in heads.items():
            if h == fid:
                continue
            f, main = by_id[fid], by_id[h]
            if main.critic_verdict == "rejected":
                continue  # основное признано ложным — дубль оцениваем по его собственному вердикту
            main.requirement_ids = sorted(set(main.requirement_ids) | set(f.requirement_ids))
            if SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[main.severity]:
                main.severity = f.severity
            f.critic_verdict = "rejected"
            f.critic_note = f"дубль замечания «{main.title}». " + f.critic_note
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
            if not a:
                continue
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

    def followup_questions(self, doc, open_findings, questions, clar_log, next_round: int):
        """Следующий раунд уточнений: новые вопросы только по замечаниям, которые остались открытыми."""
        if not open_findings:
            return []
        asked = [{"qid": q.qid, "question": q.question, "answer": q.answer} for q in questions]
        follow = [c.get("follow_up") for c in clar_log if c.get("follow_up")]
        data = self.llm.complete_json(
            f"followup-{next_round}", P.SYSTEM,
            P.fill(P.FOLLOWUP, questions_method=P.methodology("questions.md"),
                   asked=json.dumps(asked, ensure_ascii=False, indent=1),
                   follow=json.dumps(follow, ensure_ascii=False, indent=1),
                   findings=_dump(open_findings, ["fid", "requirement_ids", "severity", "title", "description", "quotes"]),
                   document=self._doc_context(doc)),
            validate=lambda d: _need(d, "questions"),
        )
        out = []
        start = len(questions)
        order = {"high": 0, "medium": 1, "low": 2}
        for q in data.get("questions", []):
            try:
                if isinstance(q.get("options"), str):
                    q["options"] = [q["options"]]
                if q.get("priority") not in order:
                    q["priority"] = "medium"
                item = Question.model_validate({k: v for k, v in q.items() if k in Question.model_fields and k != "answer"})
                item.round = next_round
                out.append(item)
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda x: order[x.priority])
        for i, q in enumerate(out, start + 1):
            q.qid = f"Q-{i:02d}"
        return out

    # ------------------------------------------------------------------ main
    def run(self, doc_path: str, answers: dict[str, str] | None = None, ask=None) -> tuple[AuditResult, Document]:
        t0 = time.time()
        started = datetime.now().astimezone()
        self.log("1/10 Чтение документа")
        doc = load_document(doc_path)
        for w in doc.warnings:
            self.log(f"  ! {w}")
        index = QuoteIndex(doc)

        self.log("2/10 Автоматические правила (без ИИ)")
        signals = run_rules(doc)
        self.log(f"  · сигналов: {len(signals)}")

        self.log("3/10 Нарезка на требования")
        reqs, title = self.extract(doc, index)
        self.log(f"  · требований: {len(reqs)}")
        no_id = [r for r in reqs if not r.doc_id or r.doc_id.startswith("(")]
        if no_id and reqs:
            # Правило без ИИ (IEEE 830, прослеживаемость): одно сводное замечание, а не по замечанию на каждое.
            signals.append(Finding(
                scope="set", criterion="TRACEABLE", rule="DOC-TRACE", severity="minor",
                title=f"[RULE-ID] {len(no_id)} из {len(reqs)} требований без идентификатора",
                description="У требований " + ", ".join(f"{r.rid} {r.doc_id}".strip() for r in no_id) +
                            " нет собственного номера в документе: на них нельзя сослаться в тестах, задачах и "
                            "при изменениях (IEEE 830, п. 4.3, прослеживаемость).",
                quotes=[r.quote[:150] for r in no_id[:3]], lines=sorted({ln for r in no_id for ln in r.lines}),
                recommendation="Присвоить каждому требованию уникальный идентификатор (например, НФ-1, Д-1, КП-1).",
                source="rule", verified=True, protected=True,
                verification_note="найдено правилом по результатам нарезки"))

        self.log("4/10 Проверка каждого требования")
        assessments, req_findings = self.assess(doc, reqs, signals)

        self.log("5/10 Проверка критериев приёмки исполнением")
        ac_checks, uncovered, ac_findings = [], [], []
        try:
            ac_checks, uncovered, ac_findings = self.ac_check(doc, reqs)
            mism = sum(c.verdict == "mismatch" for c in ac_checks)
            self.log(f"  · критериев проверено: {len(ac_checks)}, расхождений: {mism}, без критериев: {len(uncovered)}")
        except LLMError as exc:
            self.warnings.append(f"Шаг «проверка критериев приёмки» не выполнен: {exc}.")
            self.log(f"  ! {self.warnings[-1]}")

        self.log("6/10 Проход по предметным правилам методики")
        rule_checks, rp_findings = [], []
        try:
            rule_checks, rp_findings = self.rules_pass(doc, reqs)
            self.log(f"  · правил: {len(rule_checks)}, нарушено: {sum(r.status == 'violated' for r in rule_checks)}, "
                     f"не описано: {sum(r.status == 'missing' for r in rule_checks)}")
        except LLMError as exc:
            self.warnings.append(f"Шаг «предметные правила» не выполнен: {exc}.")
            self.log(f"  ! {self.warnings[-1]}")

        pc_findings = []
        try:
            pc_findings = self.pair_check(doc, reqs)
            self.log(f"  · попарная проверка требований: несовместимых пар {len(pc_findings)}")
        except LLMError as exc:
            self.warnings.append(f"Шаг «попарная проверка» не выполнен: {exc}.")
            self.log(f"  ! {self.warnings[-1]}")

        known = req_findings + ac_findings + rp_findings + pc_findings
        self.log("7/10 Проверка набора и сценарный прогон")
        try:
            set_summary, set_scores, set_findings, scenarios = self.set_review(doc, reqs, known, signals)
        except LLMError as exc:
            self.warnings.append(f"Шаг «проверка набора» не выполнен: {exc}.")
            self.log(f"  ! {self.warnings[-1]}")
            set_summary, set_scores, set_findings, scenarios = "", [], [], []

        # Двойная проверка: второй независимый проход по шагам 4, 6, 7. Находки объединяются,
        # дубли потом убирает критик. Повышает полноту ценой времени и лимита.
        for n in range(2, self.passes + 1):
            self.log(f"+ Проход {n}: повторная независимая проверка (шаги 4, 6, 7)")
            self._tag = f"-p{n}"
            self._variant = ("\n\nЭто повторная независимая проверка того же документа другим экспертом. "
                             "Особенно внимательно ищи дефекты, которые легко пропустить: расхождения между "
                             "описанием и названием, кодами и заголовками, типами полей, заголовками запроса, "
                             "между разделами документа и его частями.")
            try:
                _, f4 = self.assess(doc, reqs, signals)
                _, f6 = self.rules_pass(doc, reqs)
                f6b = self.pair_check(doc, reqs)
                _, _, f7, sc2 = self.set_review(doc, reqs, known + f4 + f6 + f6b, signals)
                extra = f4 + f6 + f6b + f7 + self._scenario_findings(sc2, reqs)
                known = known + f4 + f6 + f6b
                set_findings = set_findings + f7 + self._scenario_findings(sc2, reqs)
                self.log(f"  · проход {n}: замечаний {len(extra)} (дубли уберёт критик)")
            except LLMError as exc:
                self.warnings.append(f"Проход {n} не завершён: {exc}. Использованы результаты предыдущих проходов.")
                self.log(f"  ! {self.warnings[-1]}")
            finally:
                self._tag, self._variant = "", ""

        scen_findings = self._scenario_findings(scenarios, reqs)
        if scen_findings:
            self.log(f"  · из сценарного прогона: конфликтов и пробелов {len(scen_findings)}")

        self.log("8/10 Верификация цитат (без ИИ)")
        findings, rejected = self.verify(doc, index, known + set_findings + scen_findings)
        for f in rejected:
            f.critic_note = "отклонено верификатором: " + f.verification_note
        # Сигналы правил без ИИ уровня «существенный» и выше идут к критику вместе с остальными:
        # он отметит дубли с замечаниями модели.
        for sig in signals:
            if sig.severity in ("critical", "major") or sig.title.startswith("[RULE-ID]"):
                findings.append(sig.model_copy(deep=True))
        self.log(f"  · подтверждено цитатами: {len(findings)}, отклонено: {len(rejected)}")

        self.log("9/10 Независимый критик")
        try:
            evidence = {
                "проверка_критериев_приёмки": [c.model_dump(include={"criterion_rid", "derived_result",
                                               "stated_result", "verdict", "explanation"}) for c in ac_checks],
                "сценарный_прогон": [sc.model_dump(include={"scenario", "per_rule", "outcome", "status"})
                                    for sc in scenarios],
            }
            findings, critic_rejected = self.critic(doc, findings, json.dumps(evidence, ensure_ascii=False, indent=1))
        except LLMError as exc:
            self.warnings.append(f"Шаг «критик» не выполнен: {exc}. Замечания не прошли вторую проверку.")
            self.log(f"  ! {self.warnings[-1]}")
            critic_rejected = []
        rejected += critic_rejected
        self.log(f"  · осталось замечаний: {len(findings)}, отклонено критиком: {len(critic_rejected)}")

        # Правило без ИИ: если оба независимых пересчёта показали, что критерий приёмки совпадает
        # с требованиями, замечание «этот критерий противоречит требованиям» снимается.
        acs_by_id = {r.rid for r in reqs if "приём" in r.kind.lower() or "прием" in r.kind.lower()}
        guarded = []
        for f in findings:
            ac_ids = [x for x in f.requirement_ids if x in acs_by_id]
            contradiction = f.criterion in ("CORRECT", "SET_CONSISTENT") or "противореч" in f.title.lower()
            if contradiction and ac_ids and all(x in self.ac_all_match for x in ac_ids) and f.source != "rule":
                f.critic_verdict = "rejected"
                f.critic_note = ("снято правилом: два независимых пересчёта критерия приёмки "
                                 f"{', '.join(ac_ids)} показали совпадение с требованиями")
                rejected.append(f)
            else:
                guarded.append(f)
        if len(guarded) != len(findings):
            self.log(f"  · снято правилом пересчёта: {len(findings) - len(guarded)}")
        findings = guarded

        # Калибровка по шкале (methodology/severity.md): дефекты стиля API и окружений не выше «существенного».
        for f in findings:
            if f.severity == "critical" and (f.rule.startswith("API-") or f.rule in ("DOC-HYGIENE", "API-ENV")):
                f.severity = "major"
                f.critic_note = (f.critic_note + " " if f.critic_note else "") + "критичность ограничена шкалой: стиль API."

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

        self.log("10/10 Вопросы, чек-лист и итог")
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
            acceptance_checks=ac_checks, uncovered_requirements=uncovered, rule_checks=rule_checks,
            questions=questions, checklist=checklist, clarification_log=clar_log, verdict=verdict,
        )
        return result, doc
