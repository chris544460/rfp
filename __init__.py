# Marks rfp_utils as a package
from .rfp_xlsx_apply_answers import write_excel_answers
from .rfp_xlsx_slot_finder import extract_schema_from_xlsx, ask_sheet_schema

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

__all__.extend(["write_excel_answers"])
__all__.extend(["extract_schema_from_xlsx", "ask_sheet_schema"])
