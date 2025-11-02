"""Answering helpers focused on response generation and document filling."""

from .responder import Responder

try:  # Optional dependency: document filler needs python-docx
    from .document_filler import DocumentFiller  # noqa: WPS433 - conditional import
except ModuleNotFoundError:  # pragma: no cover - optional dependency unavailable
    DocumentFiller = None  # type: ignore[assignment]

__all__ = ["Responder"]

if DocumentFiller is not None:
    __all__.append("DocumentFiller")
