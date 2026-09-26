"""Get plain text out of an uploaded resume (PDF, DOCX, TXT/MD). Everything stays on this PC."""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

MAX_BYTES = 5 * 1024 * 1024
MAX_CHARS = 30000  # longer than any real resume; stops a huge file from reaching the model
EXTENSIONS = (".pdf", ".docx", ".txt", ".md")
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ResumeError(ValueError):
    """A user-facing reason the file could not be read."""


def extract_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if not name.endswith(EXTENSIONS):
        raise ResumeError("Upload a PDF, Word (.docx), or text file.")
    if not data:
        raise ResumeError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise ResumeError(f"The file is larger than {MAX_BYTES // 1024 // 1024} MB.")
    if name.endswith(".pdf"):
        text = _pdf(data)
    elif name.endswith(".docx"):
        text = _docx(data)
    else:
        text = _plain(data)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", "\n".join(line.strip() for line in text.splitlines())).strip()
    if len(text) < 30:
        hint = " It may be a scanned image; upload a Word or text version instead." if name.endswith(".pdf") else ""
        raise ResumeError("No readable text was found in the file." + hint)
    return text[:MAX_CHARS]


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ModuleNotFoundError as e:
        raise ResumeError("PDF support is not installed. Run: python -m pip install pypdf") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ResumeError("The PDF is password-protected. Upload an unprotected copy.")
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except ResumeError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as e:
        raise ResumeError(f"The PDF could not be read ({type(e).__name__}).") from e


def _docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        raise ResumeError("The Word file could not be read. Save it as .docx (not .doc) and try again.") from e
    paragraphs = []
    for p in root.iter(WORD_NS + "p"):
        parts = []
        for node in p.iter():
            if node.tag == WORD_NS + "t" and node.text:
                parts.append(node.text)
            elif node.tag == WORD_NS + "tab":
                parts.append("\t")
            elif node.tag in (WORD_NS + "br", WORD_NS + "cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _plain(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
