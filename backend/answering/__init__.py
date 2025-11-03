"""Answering helpers focused on response generation and document filling."""

from __future__ import annotations

from typing import Optional

from .responder import Responder

DOCUMENT_FILLER_IMPORT_ERROR: Optional[ModuleNotFoundError] = None

try:  # Optional dependency: document filler needs python-docx/openpyxl
    from .document_filler import DocumentFiller  # noqa: WPS433 - conditional import
except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency unavailable
    missing = getattr(exc, "name", None)
    print(
        "[backend.answering] DocumentFiller import failed; missing dependency:",
        repr(missing) if missing else repr(exc),
    )
    DocumentFiller = None  # type: ignore[assignment]
    DOCUMENT_FILLER_IMPORT_ERROR = exc

__all__ = ["Responder", "DOCUMENT_FILLER_IMPORT_ERROR"]

if DocumentFiller is not None:
    __all__.append("DocumentFiller")
