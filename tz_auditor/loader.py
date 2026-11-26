"""Чтение входного документа: PDF, DOCX, MD, TXT, HTML.

Задача модуля: получить текст с номерами строк и не упасть на плохом файле.
Если файл прочитать нельзя, выбрасываем LoaderError с понятным сообщением.
Если файл прочитан частично, продолжаем и пишем предупреждение.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
MIN_TEXT_CHARS = 40


class LoaderError(Exception):
    """Документ невозможно прочитать. Сообщение показывается пользователю."""


@dataclass
class Document:
    path: Path
    fmt: str
    lines: list[str]
    sha256: str
    title: str = ""
    page_of_line: dict[int, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def numbered(self, start: int = 1, end: int | None = None) -> str:
        """Текст с номерами строк: так модель может ссылаться на конкретное место."""
        end = end or len(self.lines)
        out = []
        for i in range(start, end + 1):
            line = self.lines[i - 1]
            out.append(f"L{i:04d}| {line}")
        return "\n".join(out)

    def locate(self, line: int) -> str:
        if not line:
            return ""
        if self.page_of_line:
            page = self.page_of_line.get(line)
            return f"стр. {page}, строка {line}" if page else f"строка {line}"
        return f"строка {line}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_text(data: bytes, warnings: list[str]) -> str:
    if b"\x00" in data[:4096]:
        raise LoaderError(
            "Файл похож на двоичный, а не на текстовый документ. "
            "Поддерживаются PDF, DOCX, MD, TXT, HTML."
        )
    for enc in ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None:
            warnings.append(f"Файл не в UTF-8, кодировка определена автоматически: {best.encoding}.")
            return str(best)
    except Exception:  # noqa: BLE001
        pass
    for enc in ("cp1251", "koi8_r", "latin-1"):
        try:
            text = data.decode(enc)
            warnings.append(f"Файл не в UTF-8, прочитан в кодировке {enc}. Проверьте кириллицу в отчёте.")
            return text
        except UnicodeDecodeError:
            continue
    raise LoaderError("Не удалось определить кодировку текстового файла.")


def _load_pdf(path: Path, data: bytes, warnings: list[str]) -> tuple[list[str], dict[int, int]]:
    try:
        import pymupdf  # type: ignore
    except ImportError:  # старые версии
        import fitz as pymupdf  # type: ignore

    try:
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
    except Exception:  # noqa: BLE001
        pass
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise LoaderError(f"PDF повреждён или это не PDF: {exc}") from exc

    if doc.needs_pass and not doc.authenticate(""):
        raise LoaderError("PDF защищён паролем. Снимите защиту и запустите проверку снова.")
    if doc.page_count == 0:
        raise LoaderError("В PDF нет ни одной страницы.")

    lines: list[str] = []
    page_of_line: dict[int, int] = {}
    bad_pages = []
    for pno in range(doc.page_count):
        try:
            text = doc[pno].get_text("text", sort=True)
        except Exception:  # noqa: BLE001
            bad_pages.append(pno + 1)
            continue
        for raw in text.splitlines():
            lines.append(raw.rstrip())
            page_of_line[len(lines)] = pno + 1
    if bad_pages:
        warnings.append(f"Не удалось прочитать страницы PDF: {bad_pages}. Они пропущены.")
    repaired = bool(getattr(doc, "is_repaired", False))
    if repaired:
        warnings.append("PDF был повреждён и восстановлен при чтении: часть текста могла потеряться.")
    if len("".join(lines).strip()) < MIN_TEXT_CHARS:
        if repaired:
            raise LoaderError("PDF повреждён (например, файл обрезан при загрузке): текст извлечь не удалось. "
                              "Скачайте файл заново или передайте DOCX/MD.")
        raise LoaderError(
            "В PDF почти нет текстового слоя: вероятно, это скан или картинка. "
            "Распознайте текст (OCR) или передайте документ в формате DOCX/MD."
        )
    return lines, page_of_line


def _docx_table_to_md(table) -> list[str]:
    rows = []
    for row in table.rows:
        cells = [c.text.replace("\n", " ").strip() for c in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if rows:
        ncols = rows[0].count("|") - 1
        rows.insert(1, "|" + " --- |" * ncols)
    return rows


def _load_docx(path: Path, data: bytes, warnings: list[str]) -> list[str]:
    try:
        import io

        import docx  # python-docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        d = docx.Document(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise LoaderError(f"DOCX повреждён или это не DOCX: {exc}") from exc

    lines: list[str] = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            style = (p.style.name or "").lower() if p.style is not None else ""
            if text and ("heading" in style or "заголовок" in style):
                level = re.findall(r"\d", style)
                hashes = "#" * (int(level[0]) if level else 2)
                text = f"{hashes} {text}"
            lines.append(text)
        elif tag == "tbl":
            lines.extend(_docx_table_to_md(Table(child, d)))
            lines.append("")
    return lines


def _load_html(data: bytes, warnings: list[str]) -> list[str]:
    html = _decode_text(data, warnings)
    try:
        from markdownify import markdownify

        text = markdownify(html, heading_style="ATX")
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html)
    return text.splitlines()


def _sniff(data: bytes, suffix: str) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:2] == b"PK":
        return "docx"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".md", ".markdown"}:
        return "md"
    return "txt"


def load_document(path_str: str) -> Document:
    path = Path(path_str).expanduser()
    if not path.exists():
        raise LoaderError(f"Файл не найден: {path}")
    if path.is_dir():
        raise LoaderError(f"Указана папка, а нужен файл: {path}")
    size = path.stat().st_size
    if size == 0:
        raise LoaderError(f"Файл пустой: {path}")
    if size > MAX_BYTES:
        raise LoaderError(f"Файл слишком большой ({size // 1024 // 1024} МБ). Предел: 50 МБ.")

    data = path.read_bytes()
    warnings: list[str] = []
    suffix = path.suffix.lower()
    fmt = _sniff(data, suffix)
    if suffix == ".pdf" and fmt != "pdf":
        warnings.append("Расширение .pdf, но содержимое не похоже на PDF. Пробую прочитать как текст.")
    if suffix == ".docx" and fmt != "docx":
        warnings.append("Расширение .docx, но содержимое не похоже на DOCX. Пробую прочитать как текст.")
    if suffix == ".doc":
        raise LoaderError("Старый формат .doc не поддерживается. Сохраните документ как .docx или .pdf.")

    page_of_line: dict[int, int] = {}
    if fmt == "pdf":
        lines, page_of_line = _load_pdf(path, data, warnings)
    elif fmt == "docx":
        lines = _load_docx(path, data, warnings)
    elif fmt == "html":
        lines = _load_html(data, warnings)
    else:
        lines = _decode_text(data, warnings).splitlines()

    lines = [ln.replace("\t", "    ").rstrip() for ln in lines]
    text = "\n".join(lines)
    if len(text.strip()) < MIN_TEXT_CHARS:
        raise LoaderError("В документе почти нет текста: проверять нечего.")
    bad = text.count("�")
    if bad > 20:
        warnings.append(f"В тексте {bad} нераспознанных символов: возможно, файл частично повреждён.")

    title = ""
    for ln in lines:
        s = ln.strip().lstrip("#").strip()
        if s:
            title = s[:200]
            break

    return Document(
        path=path,
        fmt=fmt,
        lines=lines,
        sha256=_sha256(data),
        title=title,
        page_of_line=page_of_line,
        warnings=warnings,
    )
