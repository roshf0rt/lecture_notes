"""Text of a PDF, for a PDF that has a text layer. A scan has none, and the bot says so."""
from __future__ import annotations

import io

from pypdf import PdfReader

MIN_CHARS = 20


class NoTextError(Exception):
    pass


def pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    except Exception as error:  # a broken or encrypted file is just as useless as a scan
        raise NoTextError(str(error)) from error
    if len(text) < MIN_CHARS:
        raise NoTextError("no text layer")
    return text
