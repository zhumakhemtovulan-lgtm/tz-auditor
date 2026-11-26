"""Тесты без обращения к модели: чтение файлов, верификатор цитат, правила, полный прогон в режиме replay.

Запуск: python -m pytest -q
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))

from tz_auditor.loader import LoaderError, load_document  # noqa: E402
from tz_auditor.rules import run_rules  # noqa: E402
from tz_auditor.verify import QuoteIndex  # noqa: E402

DOC = ROOT / "docs" / "requirements.md"


def test_load_markdown():
    d = load_document(str(DOC))
    assert d.fmt == "md" and len(d.lines) > 100


@pytest.mark.parametrize("name", ["requirements.pdf", "requirements.docx", "cp1251.txt"])
def test_load_good_files(name):
    d = load_document(str(FIX / name))
    assert len(d.text) > 100


@pytest.mark.parametrize("name", ["scan.pdf", "encrypted.pdf", "truncated.pdf", "garbage.pdf",
                                  "empty.md", "binary.txt", "fake.docx", "old.doc", "missing.md"])
def test_bad_files_give_clear_error(name):
    with pytest.raises(LoaderError):
        load_document(str(FIX / name))


def test_quote_verifier():
    qi = QuoteIndex(load_document(str(DOC)))
    ok, lines, _ = qi.find("Месяц определяется по дате совершения покупки в Алматы.")
    assert ok and lines
    ok, _, _ = qi.find("Этой фразы в документе нет и никогда не было")
    assert not ok


def test_rules_find_signals():
    titles = " ".join(f.title for f in run_rules(load_document(str(DOC))))
    assert "RULE-NFR" in titles and "RULE-UNITS" in titles


def test_full_pipeline_replay(tmp_path):
    env = dict(os.environ, LLM_PROVIDER="replay", REPLAY_DIR=str(FIX / "replay"))
    out = tmp_path / "report.md"
    r = subprocess.run([sys.executable, "-m", "tz_auditor", str(DOC), str(out), "--no-cache"],
                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    text = out.read_text("utf-8")
    for section in ("Паспорт прогона", "Как агент нарезал", "Проверка каждого требования",
                    "Проверка всего набора", "Уточняющие вопросы", "Сводка", "Итог"):
        assert section in text
    assert (tmp_path / "report.html").exists() and (tmp_path / "report.json").exists()
