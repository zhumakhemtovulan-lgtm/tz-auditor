"""Структуры данных агента. Все ответы модели проверяются по этим схемам."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "major", "minor"]

SEVERITY_RU = {"critical": "Критичный", "major": "Существенный", "minor": "Незначительный"}
SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


class Requirement(BaseModel):
    """Одно требование, выделенное из документа."""

    rid: str = Field(description="Идентификатор в отчёте, например R-01")
    doc_id: str = Field(default="", description="Идентификатор в документе (Ф-1) или пусто")
    section: str = Field(default="", description="Раздел документа")
    kind: str = Field(default="функциональное", description="Тип требования")
    summary: str = Field(description="Краткая формулировка")
    quote: str = Field(description="Дословная цитата из документа")
    lines: list[int] = Field(default_factory=list, description="Номера строк документа")
    quote_verified: bool = False


class Finding(BaseModel):
    """Одно нарушение (дефект) с привязкой к тексту."""

    fid: str = ""
    scope: Literal["requirement", "set"] = "requirement"
    requirement_ids: list[str] = Field(default_factory=list)
    criterion: str = Field(description="Код критерия качества, например UNAMBIGUOUS")
    rule: str = Field(default="", description="Код предметного правила, например FIN-CUTOFF")
    severity: Severity = "major"
    title: str = Field(description="Короткое название дефекта")
    description: str = Field(description="Что не так и почему это проблема")
    quotes: list[str] = Field(default_factory=list, description="Дословные цитаты из документа")
    lines: list[int] = Field(default_factory=list)
    impact: str = Field(default="", description="Чем опасно для банка, клиента, разработки, тестирования")
    recommendation: str = Field(default="", description="Как исправить")
    suggested_rewrite: str = Field(default="", description="Предлагаемая формулировка")
    source: Literal["llm", "rule"] = "llm"
    verified: bool = False
    verification_note: str = ""
    critic_verdict: Literal["confirmed", "rejected", "unchecked"] = "unchecked"
    critic_note: str = ""
    protected: bool = False  # жёсткое доказательство: критик не может отклонить, только дубль/критичность


class CriterionScore(BaseModel):
    criterion: str
    status: Literal["ok", "issue", "n/a"] = "ok"
    comment: str = ""


class RequirementAssessment(BaseModel):
    rid: str
    scores: list[CriterionScore] = Field(default_factory=list)
    verdict: Literal["ok", "needs_fix", "blocker"] = "ok"


class Question(BaseModel):
    qid: str = ""
    priority: Literal["high", "medium", "low"] = "medium"
    question: str
    why: str = Field(default="", description="Почему ответ важен")
    options: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    addressee: str = Field(default="Аналитик / автор ТЗ")
    answer: str = ""


class ChecklistItem(BaseModel):
    item: str
    status: Literal["Да", "Нет", "Частично", "Не требуется", "Нет данных"] = "Нет данных"
    justification: str = ""
    finding_ids: list[str] = Field(default_factory=list)


class Verdict(BaseModel):
    ready_for_development: Literal["да", "нет", "условно"] = "нет"
    ready_for_testing: Literal["да", "нет", "условно"] = "нет"
    summary: str = ""
    top_fixes: list[str] = Field(default_factory=list)


class RunPassport(BaseModel):
    document_name: str
    document_title: str = ""
    document_path: str
    document_sha256: str
    document_format: str
    run_started: str
    run_finished: str = ""
    duration_sec: float = 0.0
    provider: str
    model: str
    agent_version: str
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hits: int = 0
    warnings: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    scenario: str
    rules_applied: list[str] = Field(default_factory=list)
    per_rule: list[dict] = Field(default_factory=list)
    outcome: str = ""
    status: Literal["ok", "conflict", "gap"] = "ok"
    note: str = ""


class AcceptanceCheck(BaseModel):
    criterion_rid: str
    steps: list[dict] = Field(default_factory=list)
    derived_result: str = ""
    stated_result: str = ""
    verdict: Literal["match", "mismatch", "undetermined"] = "undetermined"
    explanation: str = ""


class RuleCheck(BaseModel):
    code: str
    status: Literal["ok", "violated", "missing", "n/a"] = "n/a"
    explanation: str = ""
    scenario: str = ""
    requirement_ids: list[str] = Field(default_factory=list)


class AuditResult(BaseModel):
    passport: RunPassport
    requirements: list[Requirement] = Field(default_factory=list)
    assessments: list[RequirementAssessment] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    rejected_findings: list[Finding] = Field(default_factory=list)
    rule_signals: list[Finding] = Field(default_factory=list)
    set_summary: str = ""
    set_scores: list[CriterionScore] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    acceptance_checks: list[AcceptanceCheck] = Field(default_factory=list)
    uncovered_requirements: list[str] = Field(default_factory=list)
    rule_checks: list[RuleCheck] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    clarification_log: list[dict] = Field(default_factory=list)
    resolved_finding_ids: list[str] = Field(default_factory=list)
    verdict_after_clarification: str = ""
    verdict: Verdict = Field(default_factory=Verdict)
