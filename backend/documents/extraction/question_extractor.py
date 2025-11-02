from __future__ import annotations

"""Composite question extraction utilities spanning Excel, DOCX, and raw text inputs."""

from pathlib import Path
from typing import Any, Dict, List, Optional

import re
from docx import Document

from backend.documents.xlsx.structured_extraction.interpreter_sheet import collect_non_empty_cells
from backend.documents.docx.slot_finder import extract_slots_from_docx
from backend.documents.xlsx.slot_finder import ask_sheet_schema
from backend.prompts import read_prompt

_EXTRACT_PROMPT = read_prompt("extract_questions")


def _load_input_text(path: str) -> str:
    """Load various document types into raw text for LLM consumption."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        try:
            from PyPDF2 import PdfReader
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("PyPDF2 is required to read PDF inputs") from exc
        # Fall back to a naive text extraction; good enough for seeded PDFs.
        out: List[str] = []
        with p.open("rb") as f:
            reader = PdfReader(f)
            for page in reader.pages:
                out.append(page.extract_text() or "")
                # Older PDFs can return None for blank pages—defaulting to "" keeps alignment.
        return "\n".join(out)
    if suffix in {".doc", ".docx"}:
        doc = Document(p)
        return "\n".join(par.text for par in doc.paragraphs)
    return p.read_text(encoding="utf-8")


def _extract_questions(text: str, llm_client) -> List[str]:
    """Call the LLM prompt and parse numbered question lines out of the response."""
    # Use the shared prompt so the LLM extracts numbered lines the UI knows how
    # to parse. We trim the leading numerals here.
    prompt = _EXTRACT_PROMPT.format(text=text)
    result = llm_client.get_completion(prompt)
    if isinstance(result, tuple):
        response = result[0]
    else:
        response = result
    lines = str(response or "").splitlines()
    questions: List[str] = []
    for line in lines:
        m = re.match(r"^\s*\d+\)\s+(.*)\s*$", line)
        if m:
            questions.append(m.group(1).strip())
        # Ignore unnumbered lines so the caller receives a clean question list.
    return questions


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
        """High-level entry point that routes to Excel, DOCX, or text logic."""
        path_obj = Path(path)
        suffix = path_obj.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            return self._extract_from_excel(path_obj)
        if suffix == ".docx" and not treat_docx_as_text:
            return self._extract_from_docx_slots(path_obj)
        # Fallback: load the raw text (PDF/Docx/plain) and ask the LLM to tease
        # out numbered questions.
        return self.extract_from_text(_load_input_text(str(path_obj)), source=str(path_obj))

    def extract_from_text(self, text: str, *, source: Optional[str] = None) -> List[Dict[str, Any]]:
        """Ask the LLM to pull numbered questions out of arbitrary text strings."""
        if self._llm is None:
            raise ValueError("QuestionExtractor requires an LLM client for text extraction.")
        questions = _extract_questions(text, self._llm)
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
        """Leverage the Excel schema pipeline to produce question payloads."""
        # Warm the interpreter cache so worksheet-level heuristics run only once.
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
        # Persist the last run details so the UI can render a structured summary.
        self._last_details = {
            "mode": "excel",
            "schema": schema,
            "count": len(questions),
            "path": str(path),
        }
        return questions

    def _extract_from_docx_slots(self, path: Path) -> List[Dict[str, Any]]:
        """Reuse the DOCX slot finder so we get consistent metadata everywhere."""
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
        # Capture skipped slots for debugging so integrators can inspect misfires.
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


# if __name__ == "__main__":
#     from backend.llm.completions_client import CompletionsClient
#     extractor = QuestionExtractor(llm_client=CompletionsClient())
#     sample = extractor.extract("samples/questionnaire.docx")
#     print(f"Extracted {len(sample)} questions")
