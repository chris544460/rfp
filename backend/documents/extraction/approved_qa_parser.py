"""Parse approved RFP/DDQ answer libraries into normalized Q/A records."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from backend.retrieval.stacks.faiss.structured_extraction.parser import (
    ExcelAnswerLibraryParser,
    ExcelQuestionnaireParser,
    LoopioExcelParser,
    MixedDocParser,
)

logger = logging.getLogger(__name__)


@dataclass
class AnswerVariant:
    """Single answer option attached to a question."""

    key: str
    value: str
    is_primary: bool = True
    language_code: str = "en"


@dataclass
class QARecord:
    """Normalized representation of a question plus associated answers."""

    question: str
    answers: List[AnswerVariant] = field(default_factory=list)
    alternate_questions: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    source: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)

    def to_responsive_payload(self) -> Dict[str, object]:
        """Return a dict shaped like POST /answer-lib/add expects."""
        if not self.question or not self.answers:
            raise ValueError(
                "QARecord requires both question and answer text to serialize."
            )
        payload = {
            "question": self.question,
            "alternateQuestions": list(self.alternate_questions),
            "answers": [
                {
                    "key": ans.key,
                    "value": ans.value,
                    "isPrimary": ans.is_primary,
                    "languageCode": ans.language_code,
                }
                for ans in self.answers
                if ans.value
            ],
            "tags": list(self.tags),
        }
        if not payload["answers"]:
            raise ValueError(
                "QARecord cannot serialize when answers are empty after filtering."
            )
        return payload


class ApprovedQAParser:
    """Parse approved answer libraries into structured Q/A slots."""

    def __init__(
        self,
        *,
        default_answer_key: str = "Answer",
        default_language: str = "en",
    ) -> None:
        self.default_answer_key = default_answer_key.strip() or "Answer"
        self.default_language = default_language.strip() or "en"

    def parse(
        self,
        source: Union[str, Path, BinaryIO, bytes],
        *,
        file_name: Optional[str] = None,
    ) -> List[QARecord]:
        """Materialize the input artifact and route to the right parser."""
        path, cleanup = self._materialize(source, file_name=file_name)
        suffix = Path(path).suffix.lower()
        try:
            if suffix in {".xlsx", ".xls"}:
                return self._parse_excel(path)
            if suffix == ".docx":
                return self._parse_docx(path)
            raise ValueError(
                f"Unsupported file type '{suffix}' for approved QA parsing."
            )
        finally:
            if cleanup:
                try:
                    os.unlink(path)
                except OSError:
                    logger.debug("Failed to cleanup temp file %s", path, exc_info=True)

    def to_responsive_payload(
        self, records: Sequence[QARecord]
    ) -> List[Dict[str, object]]:
        """Convert parsed records into the Responsive upload schema."""
        payload: List[Dict[str, object]] = []
        for record in records:
            try:
                payload.append(record.to_responsive_payload())
            except ValueError as exc:
                logger.debug("Skipping record during serialization: %s", exc)
        return payload

    # ── Excel parsing -----------------------------------------------------

    def _parse_excel(self, path: str) -> List[QARecord]:
        """Attempt each supported Excel layout until one returns records.

        Args:
            path: Filesystem path to the Excel artifact.

        Returns:
            List of QARecord entries parsed from the file (may be empty).
        """
        parsers = (
            self._parse_excel_answer_library,
            self._parse_excel_loopio,
            self._parse_excel_questionnaire,
        )
        last_error: Optional[Exception] = None
        for handler in parsers:
            try:
                records = handler(path)
            except (ValueError, FileNotFoundError) as exc:
                last_error = exc
                continue
            if records:
                return records
        if last_error:
            logger.debug("Excel parsing failed: %s", last_error, exc_info=True)
        return []

    def _parse_excel_answer_library(self, path: str) -> List[QARecord]:
        """Parse Answer Library-style exports into QARecord objects.

        Args:
            path: Filesystem path to the workbook.

        Returns:
            List of QARecord instances detected from the sheet.
        """
        parser = ExcelAnswerLibraryParser(path)
        raw = parser.parse()
        return [
            self._build_record(
                question=entry.get("question", ""),
                answers=self._build_answer_variants(entry.get("answers") or []),
                alternate=entry.get("alternate_questions") or [],
                tags=entry.get("tags") or [],
                source=entry.get("source"),
                metadata={
                    "id": entry.get("id"),
                    "section": entry.get("section"),
                    "yes_no": entry.get("yes_no"),
                },
            )
            for entry in raw
            if entry.get("question") and entry.get("answers")
        ]

    def _parse_excel_loopio(self, path: str) -> List[QARecord]:
        """Parse Loopio Excel exports into QARecord objects.

        Args:
            path: Filesystem path to the workbook.

        Returns:
            List of QARecord instances detected from the sheet.
        """
        parser = LoopioExcelParser(path)
        raw = parser.parse()
        return [
            self._build_record(
                question=entry.get("question", ""),
                answers=self._build_answer_variants(entry.get("answers") or []),
                alternate=entry.get("alternate_questions") or [],
                tags=entry.get("tags") or [],
                source=entry.get("source"),
                metadata={
                    "id": entry.get("id"),
                    "section": entry.get("section"),
                },
            )
            for entry in raw
            if entry.get("question") and entry.get("answers")
        ]

    def _parse_excel_questionnaire(self, path: str) -> List[QARecord]:
        """Parse two-column questionnaire spreadsheets into QARecord objects.

        Args:
            path: Filesystem path to the workbook.

        Returns:
            List of QARecord entries built from two-column Q/A rows.
        """
        parser = ExcelQuestionnaireParser(path)
        raw = parser.parse()
        return [
            self._build_record(
                question=entry.get("field", ""),
                answers=self._build_answer_variants(
                    [entry.get("value", "")] if entry.get("value") else []
                ),
                alternate=[],
                tags=[],
                source=entry.get("source"),
                metadata={"section": entry.get("section")},
            )
            for entry in raw
            if entry.get("field") and entry.get("value")
        ]

    # ── DOCX parsing ------------------------------------------------------

    def _parse_docx(self, path: str) -> List[QARecord]:
        """Parse DOCX tables (and paragraph-form Q/A) into QARecord objects.

        Args:
            path: Filesystem path to the DOCX artifact.

        Returns:
            List of QARecord entries extracted from tables/paragraphs.
        """
        parser = MixedDocParser(path)
        raw = parser.parse()
        records: List[QARecord] = []

        pending_question: Optional[str] = None
        pending_lines: List[str] = []
        pending_meta: Dict[str, object] = {}

        def flush_pending() -> None:
            """Persist the currently buffered paragraph Q/A pair if possible."""
            nonlocal pending_question, pending_lines, pending_meta
            if pending_question and pending_lines:
                answer_text = "\n".join(pending_lines).strip()
                if answer_text:
                    records.append(
                        self._build_record(
                            question=pending_question,
                            answers=self._build_answer_variants([answer_text]),
                            alternate=[],
                            tags=[],
                            source=pending_meta.get("source"),
                            metadata={"section": pending_meta.get("section")},
                        )
                    )
            pending_question = None
            pending_lines = []
            pending_meta = {}

        for entry in raw:
            entry_type = entry.get("type")
            if entry_type == "table_qa":
                flush_pending()
                question = (entry.get("field") or "").strip()
                answer = (entry.get("value") or "").strip()
                if not question or not answer:
                    continue
                records.append(
                    self._build_record(
                        question=question,
                        answers=self._build_answer_variants([answer]),
                        alternate=[],
                        tags=[],
                        source=entry.get("source"),
                        metadata={"section": entry.get("section")},
                    )
                )
                continue

            if entry_type == "heading":
                flush_pending()
                continue

            if entry_type == "paragraph":
                text = (entry.get("text") or "").strip()
                if not text:
                    continue
                if self._looks_like_question(text):
                    flush_pending()
                    pending_question = self._strip_question_prefix(text)
                    pending_meta = {
                        "section": entry.get("section"),
                        "source": entry.get("source"),
                    }
                    pending_lines = []
                elif pending_question:
                    pending_lines.append(text)

        flush_pending()
        return records

    # ── Helpers -----------------------------------------------------------

    def _build_answer_variants(self, values: Iterable[str]) -> List[AnswerVariant]:
        """Normalize raw strings into AnswerVariant entries with default keys.

        Args:
            values: Iterable of answer strings gathered from the source.

        Returns:
            List of AnswerVariant entries (primary is the first non-empty value).
        """
        variants: List[AnswerVariant] = []
        for idx, raw_value in enumerate(values):
            text = (raw_value or "").strip()
            if not text:
                continue
            suffix = "" if idx == 0 else f"_{idx + 1}"
            variants.append(
                AnswerVariant(
                    key=f"{self.default_answer_key}{suffix}",
                    value=text,
                    is_primary=idx == 0,
                    language_code=self.default_language,
                )
            )
        return variants

    @staticmethod
    def _build_record(
        *,
        question: str,
        answers: List[AnswerVariant],
        alternate: Sequence[str],
        tags: Sequence[str],
        source: Optional[str],
        metadata: Optional[Dict[str, object]],
    ) -> QARecord:
        """Construct a QARecord while stripping whitespace and empty values.

        Args:
            question: Question text.
            answers: List of AnswerVariant objects.
            alternate: Alternate question phrasings.
            tags: Associated tags.
            source: Optional source identifier.
            metadata: Optional metadata captured from the parser.

        Returns:
            QARecord containing the supplied data.
        """
        return QARecord(
            question=question.strip(),
            answers=answers,
            alternate_questions=[a.strip() for a in alternate if a and str(a).strip()],
            tags=[t.strip() for t in tags if t and str(t).strip()],
            source=source,
            metadata={k: v for k, v in (metadata or {}).items() if v is not None},
        )

    @staticmethod
    def _materialize(
        source: Union[str, Path, BinaryIO, bytes],
        *,
        file_name: Optional[str],
    ) -> Tuple[str, bool]:
        """
        Copy input data into a temp file if needed.

        Args:
            source: Path, bytes, or binary stream containing the document.
            file_name: Optional hint for deriving a suffix.

        Returns:
            Tuple[path, cleanup] where cleanup indicates the caller should
            delete the temp file after use.

        Raises:
            FileNotFoundError: When the provided path does not exist.
            TypeError: If the stream type is unsupported.
            ValueError: When the data is empty.
        """
        if isinstance(source, (str, Path)):
            path = Path(source)
            if not path.exists():
                raise FileNotFoundError(f"Approved QA source '{path}' does not exist.")
            return str(path), False

        data: bytes
        if isinstance(source, bytes):
            data = source
        else:
            # The object is likely a BytesIO/UploadedFile. Read without assuming seek(0).
            buffer = source  # type: ignore[assignment]
            if hasattr(buffer, "read"):
                data = buffer.read()
            else:
                raise TypeError(
                    "ApprovedQAParser expects a path, bytes, or binary stream."
                )
        if not data:
            raise ValueError("Approved QA source is empty.")

        suffix = Path(file_name or "").suffix or ".tmp"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(data)
            temp_path = tmp.name
        return temp_path, True

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        """Heuristic check for whether a paragraph resembles a question prompt.

        Args:
            text: Paragraph text to inspect.

        Returns:
            True if the string appears to represent a question, False otherwise.
        """
        cleaned = text.strip()
        if not cleaned:
            return False
        if cleaned.endswith("?"):
            return True
        lowered = cleaned.lower()
        if lowered.startswith(("question ", "question:", "q:", "q.")):
            return True
        if ":" in cleaned:
            prefix = cleaned.split(":", 1)[0].strip().lower()
            if prefix.startswith("question"):
                return True
        return False

    @staticmethod
    def _strip_question_prefix(text: str) -> str:
        """Remove 'Question:'-style prefixes when building normalized text.

        Args:
            text: Paragraph text potentially prefixed with numbering.

        Returns:
            Question text without leading numbering/prefixes.
        """
        cleaned = text.strip()
        lowered = cleaned.lower()
        if lowered.startswith("question"):
            parts = cleaned.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
            parts = cleaned.split(".", 1)
            if len(parts) == 2:
                return parts[1].strip()
        if cleaned.lower().startswith("q:"):
            return cleaned[2:].strip()
        if cleaned.endswith("?"):
            return cleaned
        return cleaned


__all__ = ["ApprovedQAParser", "QARecord", "AnswerVariant"]


# Example usage:
# if __name__ == "__main__":
#     parser = ApprovedQAParser()
#     records = parser.parse("path/to/approved_library.xlsx")
#     payload = parser.to_responsive_payload(records)
#     print(f"Parsed {len(records)} QA pairs; first entry: {payload[0] if payload else 'N/A'}")
