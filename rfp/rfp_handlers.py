"""File type handler registry for slot extraction and answer application.

This module centralizes the mapping between file extensions and the
functions that know how to extract question/answer slots and how to
apply answers back to the document.  New file formats can be supported
simply by registering the appropriate functions here without modifying
the core pipeline.
"""
from __future__ import annotations

from importlib import import_module
from typing import Callable, Dict, Tuple

# Type aliases for clarity
SlotExtractor = Callable[[str], dict]
AnswerApplier = Callable[[str, str, str, str], dict]

# Registry mapping lower‑case file extensions to the modules and function
# names that implement the required operations for that format.
#
#   extension: (slot_finder_module, slot_finder_func,
#               answer_applier_module, answer_applier_func)
#
FILE_HANDLERS: Dict[str, Tuple[str, str, str, str]] = {
    ".docx": (
        "rfp_docx_slot_finder", "extract_slots_from_docx",
        "rfp_docx_apply_answers", "apply_answers_to_docx",
    ),
    ".xlsx": (
        "rfp_xlsx_slot_finder", "extract_slots_from_xlsx",
        "rfp_xlsx_apply_answers", "apply_answers_to_xlsx",
    ),
}


def get_handlers(ext: str) -> Tuple[SlotExtractor, AnswerApplier]:
    """Return (slot_extractor, answer_applier) for the given extension.

    Raises ``ValueError`` if the extension is unknown.  Extensions should
    include the leading dot, e.g. ``.docx``.
    """
    key = ext.lower()
    if key not in FILE_HANDLERS:
        raise ValueError(f"Unsupported file extension: {ext}")
    slot_mod, slot_func, apply_mod, apply_func = FILE_HANDLERS[key]
    slot_extractor = getattr(import_module(slot_mod), slot_func)
    answer_applier = getattr(import_module(apply_mod), apply_func)
    return slot_extractor, answer_applier
