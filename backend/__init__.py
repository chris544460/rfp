"""Convenience exports for the backend package.

The backend codebase is organized by capability (answering, documents,
retrieval, storage, etc.).  This module re-exports the most commonly used
entry points so existing imports such as ``from backend import Responder`` keep
working while still allowing direct imports from the more descriptive
subpackages.
"""

from .answering import Responder, conversation

try:
    from .answering import DocumentFiller
except ModuleNotFoundError:  # pragma: no cover - optional docx dependency
    DocumentFiller = None  # type: ignore[assignment]
from .answering.qa_engine import answer_question, collect_relevant_snippets
try:
    from .documents.docx.apply_answers import apply_answers_to_docx
except ModuleNotFoundError:  # pragma: no cover - optional dependency unavailable
    apply_answers_to_docx = None  # type: ignore[assignment]

try:
    from .documents.docx.slot_finder import extract_slots_from_docx
except ModuleNotFoundError:  # pragma: no cover - optional dependency unavailable
    extract_slots_from_docx = None  # type: ignore[assignment]

try:
    from .documents.xlsx.apply_answers import write_excel_answers
except ModuleNotFoundError:  # pragma: no cover - optional dependency unavailable
    write_excel_answers = None  # type: ignore[assignment]

try:
    from .documents.xlsx.slot_finder import (
        ask_sheet_schema,
        extract_schema_from_xlsx,
        extract_slots_from_xlsx,
    )
except ModuleNotFoundError:  # pragma: no cover - optional dependency unavailable
    ask_sheet_schema = extract_schema_from_xlsx = extract_slots_from_xlsx = None  # type: ignore[assignment]
try:
    from .document_jobs.controller import DocumentJobController
except ModuleNotFoundError:  # pragma: no cover - optional streamlit dependency
    DocumentJobController = None  # type: ignore[assignment]

from .llm.completions_client import CompletionsClient, get_openai_completion

try:  # Optional dependency: QuestionExtractor pulls in spaCy at import time
    from .answering import QuestionExtractor  # noqa: WPS433 - conditional import
except ModuleNotFoundError:  # pragma: no cover - optional dependency unavailable
    QuestionExtractor = None  # type: ignore[assignment]

__all__ = [
    "Responder",
    "conversation",
    "answer_question",
    "collect_relevant_snippets",
    "CompletionsClient",
    "get_openai_completion",
]

if DocumentFiller is not None:
    __all__.append("DocumentFiller")

if DocumentJobController is not None:
    __all__.append("DocumentJobController")

if QuestionExtractor is not None:  # pragma: no branch - simple guard
    __all__.append("QuestionExtractor")

if apply_answers_to_docx is not None:
    __all__.append("apply_answers_to_docx")
if extract_slots_from_docx is not None:
    __all__.append("extract_slots_from_docx")
if write_excel_answers is not None:
    __all__.append("write_excel_answers")
if ask_sheet_schema is not None:
    __all__.append("ask_sheet_schema")
if extract_schema_from_xlsx is not None:
    __all__.append("extract_schema_from_xlsx")
if extract_slots_from_xlsx is not None:
    __all__.append("extract_slots_from_xlsx")
