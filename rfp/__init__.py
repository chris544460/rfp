# Marks rfp_utils as a package
__all__ = [
    "qa_core",
    "my_module",
    "rfp_docx_apply_answers",
    "rfp_docx_slot_finder",
    "rfp_xlsx_slot_finder",
    "rfp_xlsx_apply_answers",
    "rfp_pipeline",
    "rfp_handlers",
    "answer_composer",
    "word_comments",
]

try:
    from .rfp_xlsx_apply_answers import write_excel_answers

    __all__.append("write_excel_answers")
except Exception:  # pragma: no cover - optional dependency not available
    write_excel_answers = None  # type: ignore

try:
    from .rfp_xlsx_slot_finder import extract_schema_from_xlsx, ask_sheet_schema

    __all__.extend(["extract_schema_from_xlsx", "ask_sheet_schema"])
except Exception:  # pragma: no cover - optional dependency not available
    extract_schema_from_xlsx = None  # type: ignore
    ask_sheet_schema = None  # type: ignore
