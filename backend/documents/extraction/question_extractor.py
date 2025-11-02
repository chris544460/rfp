from __future__ import annotations

"""Composite question extraction utilities spanning Excel, DOCX, and raw text inputs."""

import io
import re
from typing import Any, Dict, IO, List, Optional, Tuple

from docx import Document

from backend.documents.xlsx.structured_extraction.interpreter_sheet import collect_non_empty_cells
from backend.documents.docx.slot_finder import extract_slots_from_docx
from backend.documents.xlsx.slot_finder import ask_sheet_schema
from backend.prompts import read_prompt

_EXTRACT_PROMPT = read_prompt("extract_questions")


def _clone_buffer(data: bytes, name: Optional[str]) -> io.BytesIO:
    """Return a fresh BytesIO view of ``data`` with an optional name attribute."""
    buffer = io.BytesIO(data)
    if name:
        try:
            buffer.name = name  # type: ignore[attr-defined]
        except Exception:
            pass
    return buffer


def _read_stream(stream: IO[bytes]) -> Tuple[bytes, Optional[str]]:
    """Read an in-memory binary stream without disturbing caller file pointers."""
    name = getattr(stream, "name", None)
    try:
        position = stream.tell()
    except Exception:
        position = None
    try:
        stream.seek(0)
    except Exception:
        pass
    data = stream.read()
    if position is not None:
        try:
            stream.seek(position)
        except Exception:
            pass
    return data, name


def _suffix_from_name(name: Optional[str]) -> str:
    """Return a lowercase suffix (including leading dot) derived from a stream name."""
    if not name:
        return ""
    # Support names that include directories; only inspect the final component.
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = base.rfind(".")
    if dot == -1:
        return ""
    return base[dot:].lower()


def _load_input_text(buffer: io.BytesIO, *, suffix: str) -> str:
    """Load various document types into raw text for LLM consumption."""
    if suffix == ".pdf":
        try:
            from PyPDF2 import PdfReader
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("PyPDF2 is required to read PDF inputs") from exc
        # Fall back to a naive text extraction; good enough for seeded PDFs.
        out: List[str] = []
        buffer.seek(0)
        reader = PdfReader(buffer)
        for page in reader.pages:
            out.append(page.extract_text() or "")
            # Older PDFs can return None for blank pages—defaulting to "" keeps alignment.
        return "\n".join(out)
    if suffix in {".doc", ".docx"}:
        buffer.seek(0)
        doc = Document(buffer)
        return "\n".join(par.text for par in doc.paragraphs)
    buffer.seek(0)
    return buffer.read().decode("utf-8")


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

    def extract(self, stream: IO[bytes], *, treat_docx_as_text: bool = False) -> List[Dict[str, Any]]:
        """High-level entry point that routes to Excel, DOCX, or text logic."""
        data, raw_name = _read_stream(stream)
        suffix = _suffix_from_name(raw_name)
        source_name = raw_name or "in-memory"

        if suffix in {".xlsx", ".xls"}:
            return self._extract_from_excel(data, source_name=source_name)
        if suffix == ".docx" and not treat_docx_as_text:
            return self._extract_from_docx_slots(data, source_name=source_name)
        # Fallback: load the raw text (PDF/Docx/plain) and ask the LLM to tease
        # out numbered questions.
        text = _load_input_text(_clone_buffer(data, raw_name), suffix=suffix)
        return self.extract_from_text(text, source=source_name)

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

    def _extract_from_excel(self, data: bytes, *, source_name: str) -> List[Dict[str, Any]]:
        """Leverage the Excel schema pipeline to produce question payloads."""
        # Warm the interpreter cache so worksheet-level heuristics run only once.
        collect_non_empty_cells(_clone_buffer(data, source_name))
        schema = ask_sheet_schema(_clone_buffer(data, source_name))
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
            "path": source_name,
        }
        return questions

    def _extract_from_docx_slots(self, data: bytes, *, source_name: str) -> List[Dict[str, Any]]:
        """Reuse the DOCX slot finder so we get consistent metadata everywhere."""
        payload = extract_slots_from_docx(_clone_buffer(data, source_name))
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
            "path": source_name,
        }
        return questions


# if __name__ == "__main__":
#     from backend.llm.completions_client import CompletionsClient
#     extractor = QuestionExtractor(llm_client=CompletionsClient())
#     with open("samples/questionnaire.docx", "rb") as fh:
#         sample = extractor.extract(fh)
#     print(f"Extracted {len(sample)} questions")
