"""High-level answering helpers (extraction, response generation, document fill)."""

from .responder import Responder

try:  # Optional dependency: question extraction pulls in docx + spaCy
    from .question_extractor import QuestionExtractor  # noqa: WPS433 - conditional import
except ModuleNotFoundError:  # pragma: no cover - optional dependency unavailable
    QuestionExtractor = None  # type: ignore[assignment]

try:  # Optional dependency: document filler needs python-docx
    from .document_filler import DocumentFiller  # noqa: WPS433 - conditional import
except ModuleNotFoundError:  # pragma: no cover - optional dependency unavailable
    DocumentFiller = None  # type: ignore[assignment]

__all__ = ["Responder"]

if QuestionExtractor is not None:
    __all__.append("QuestionExtractor")
if DocumentFiller is not None:
    __all__.append("DocumentFiller")
