"""DOCX-specific parsing and rendering helpers."""

from .answer_writer import apply_answers_to_docx
from .slot_extractor import extract_slots_from_docx

__all__ = ["apply_answers_to_docx", "extract_slots_from_docx"]
