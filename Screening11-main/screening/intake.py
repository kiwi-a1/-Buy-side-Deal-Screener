"""Turn uploaded documents into plain text, with validation that gives the user a clear reason when it can't."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree


class IntakeError(ValueError):
    """Raised with a message that is safe to show to the user."""


MIN_CHARS = 200        # below this a PDF is almost certainly scanned images (no OCR, on purpose: keeps setup simple)
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_RESOURCES = 5
RESOURCE_TYPES = {".pdf", ".docx", ".txt", ".md", ".csv"}


def clean(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pdf_text(data: bytes, label: str = "slide") -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
    if not data.startswith(b"%PDF"):
        raise IntakeError("This file is not a PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise IntakeError("This PDF is password-protected. Remove the password and try again.")
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except IntakeError:
        raise
    except (PyPdfError, ValueError, OSError, KeyError, TypeError) as e:
        raise IntakeError(f"Could not read this PDF ({type(e).__name__}).") from e
    text = clean("\n\n".join(f"--- {label} {i} ---\n{t}" for i, t in enumerate(pages, 1) if t))
    if len(text) < MIN_CHARS:
        raise IntakeError("No readable text found - this PDF looks scanned (images only). Export it from the original file.")
    return text


def docx_text(data: bytes) -> str:
    """Word files are zip archives; the body text lives in word/document.xml (no extra dependency needed)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        raise IntakeError("Could not read this Word file.") from e
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = ["".join(t.text or "" for t in p.iter(f"{ns}t")) for p in root.iter(f"{ns}p")]
    return clean("\n".join(p for p in paras if p.strip()))


def resource_text(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    if ext not in RESOURCE_TYPES:
        raise IntakeError(f"{name}: unsupported file type. Use {', '.join(sorted(RESOURCE_TYPES))}.")
    if ext == ".pdf":
        return pdf_text(data, label="page")
    if ext == ".docx":
        return docx_text(data)
    return clean(data.decode("utf-8", errors="replace"))


def check_upload(name: str, data: bytes, deck: bool = False) -> None:
    """Fast checks before a job is queued, so obvious mistakes are reported immediately."""
    if not data:
        raise IntakeError(f"{name}: the file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise IntakeError(f"{name}: over 20 MB.")
    ext = Path(name).suffix.lower()
    if deck and (ext != ".pdf" or not data.startswith(b"%PDF")):
        raise IntakeError("The pitch deck must be a PDF.")
    if not deck and ext not in RESOURCE_TYPES:
        raise IntakeError(f"{name}: unsupported file type. Use {', '.join(sorted(RESOURCE_TYPES))}.")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise IntakeError("Enter the company's website.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if "." not in host or " " in url or len(url) > 2000:
        raise IntakeError("That does not look like a website address.")
    return url


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Keep the start and the end: decks put team, round size and contacts on the last slides."""
    if len(text) <= max_chars:
        return text, False
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return text[:head].rstrip() + "\n[... MIDDLE OMITTED - free-tier token limit ...]\n" + text[-tail:].lstrip(), True
