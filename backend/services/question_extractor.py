from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from cli_app import extract_questions, load_input_text
from input_file_reader.interpreter_sheet import collect_non_empty_cells
from ..rfp_docx_slot_finder import extract_slots_from_docx
from ..rfp_xlsx_slot_finder import ask_sheet_schema


class QuestionExtractor:
    """Facade around the various question-extraction entry points in the codebase."""

    def __init__(self, llm_client=None):
        self._llm = llm_client
        self._last_details: Dict[str, Any] = {}

    @property
    def last_details(self) -> Dict[str, Any]:
        """Metadata captured during the most recent extraction call."""
        return self._last_details

    def extract(self, path: str, *, treat_docx_as_text: bool = False) -> List[Dict[str, Any]]:
        path_obj = Path(path)
        suffix = path_obj.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            return self._extract_from_excel(path_obj)
        if suffix == ".docx" and not treat_docx_as_text:
            return self._extract_from_docx_slots(path_obj)
        return self.extract_from_text(load_input_text(str(path_obj)), source=str(path_obj))

    def extract_from_text(self, text: str, *, source: Optional[str] = None) -> List[Dict[str, Any]]:
        if self._llm is None:
            raise ValueError("QuestionExtractor requires an LLM client for text extraction.")
        questions = extract_questions(text, self._llm)
        payload = [
            {
                "question": q,
                "source": source or "text",
                "index": idx,
            }
            for idx, q in enumerate(questions)
        ]
        self._last_details = {
            "mode": "text",
            "source": source,
            "count": len(payload),
        }
        return payload

    def _extract_from_excel(self, path: Path) -> List[Dict[str, Any]]:
        collect_non_empty_cells(str(path))
        schema = ask_sheet_schema(str(path))
        questions = []
        for entry in schema:
            question_text = (entry.get("question_text") or "").strip()
            questions.append(
                {
                    "question": question_text,
                    "source": "excel",
                    "schema_entry": entry,
                }
            )
        self._last_details = {
            "mode": "excel",
            "schema": schema,
            "count": len(questions),
            "path": str(path),
        }
        return questions

    def _extract_from_docx_slots(self, path: Path) -> List[Dict[str, Any]]:
        payload = extract_slots_from_docx(str(path))
        slots = payload.get("slots") or []
        questions = []
        for slot in slots:
            question_text = (slot.get("question_text") or "").strip()
            questions.append(
                {
                    "question": question_text,
                    "source": "docx_slots",
                    "slot_id": slot.get("id"),
                    "slot": slot,
                }
            )
        skipped = payload.get("skipped_slots") or []
        heuristic = payload.get("heuristic_skips") or []
        self._last_details = {
            "mode": "docx_slots",
            "slots_payload": payload,
            "skipped_slots": skipped,
            "heuristic_skips": heuristic,
            "count": len(questions),
            "path": str(path),
        }
        return questions
