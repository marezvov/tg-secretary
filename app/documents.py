"""Text extraction from PDF / DOCX / TXT / Markdown attachments."""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MAX_CHARS = 60000


class DocumentError(RuntimeError):
    pass


def extract_document_text(data: bytes, filename: str = "document") -> tuple[str, str]:
    """Return (title, text). Raises DocumentError for unsupported/broken files."""
    name = filename or "document"
    suffix = Path(name).suffix.lower()
    title = Path(name).stem or "Документ"
    if not data:
        raise DocumentError("Пустой файл")
    if suffix == ".pdf":
        text = _pdf_text(data)
    elif suffix in {".docx", ".doc"}:
        if suffix == ".doc":
            raise DocumentError("Старый формат .doc не поддерживается — сохрани как .docx или PDF.")
        text = _docx_text(data)
    elif suffix in {".txt", ".md", ".markdown", ".csv", ".json", ".log"}:
        text = _plain_text(data)
    else:
        # Last attempt: some Telegram files arrive without a useful extension.
        if data.lstrip().startswith(b"%PDF"):
            text = _pdf_text(data)
        elif data.lstrip()[:2] == b"PK":
            text = _docx_text(data)
        else:
            text = _plain_text(data)
    text = _clean(text)
    if len(text) < 20:
        raise DocumentError(
            "В документе не нашлось текстового слоя. Если это скан, отправь его как фото."
        )
    return title, text[:MAX_CHARS]


def _pdf_text(data: bytes) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise DocumentError("PyMuPDF не установлен: pip install pymupdf") from exc
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"Не удалось открыть PDF: {exc}") from exc
    parts: list[str] = []
    try:
        for page_number, page in enumerate(document):
            if page_number >= 200:
                parts.append("\n[... документ обрезан: больше 200 страниц ...]")
                break
            parts.append(page.get_text("text"))
        if not any(part.strip() for part in parts):
            parts.append(document.metadata.get("title", "") if document.metadata else "")
    finally:
        document.close()
    return "\n\n".join(parts)


def _docx_text(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise DocumentError("python-docx не установлен: pip install python-docx") from exc
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"Не удалось открыть DOCX: {exc}") from exc
    chunks: list[str] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if text:
            chunks.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                chunks.append(" | ".join(cells))
    return "\n".join(chunks)


def _plain_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()
