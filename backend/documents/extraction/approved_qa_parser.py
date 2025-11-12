"""Parse approved RFP/DDQ answer documents into normalized Q/A records."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple, Union

try:  # Optional dependency; docx parsing only.
    from docx import Document as _DOCX_DOCUMENT
    from docx.table import Table as _DOCX_TABLE
    from docx.text.paragraph import Paragraph as _DOCX_PARAGRAPH
    from docx.oxml.text.paragraph import CT_P as _DOCX_CTP
    from docx.oxml.table import CT_Tbl as _DOCX_CTTBL
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    _DOCX_DOCUMENT = None  # type: ignore[assignment]
    _DOCX_TABLE = None  # type: ignore[assignment]
    _DOCX_PARAGRAPH = None  # type: ignore[assignment]
    _DOCX_CTP = None  # type: ignore[assignment]
    _DOCX_CTTBL = None  # type: ignore[assignment]

try:  # Optional spaCy dependency for question detection.
    import spacy as _SPACY
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    _SPACY = None

QUESTION_PREFIX_RE = re.compile(r"^(question\s*\d+[:.\-]|\bq[:.\-])\s*", re.IGNORECASE)
NUMBERED_PREFIX_RE = re.compile(r"^(\d+[\).\s]+|[a-z][\).\s]+)", re.IGNORECASE)
QUESTION_WORDS = {"who", "what", "when", "where", "why", "how", "which"}

if _SPACY is not None:  # pragma: no cover - exercised via runtime
    try:
        _NLP = _SPACY.load("en_core_web_sm")
    except Exception:
        _NLP = _SPACY.blank("en")
        if "sentencizer" not in _NLP.pipe_names:
            _NLP.add_pipe("sentencizer")
else:  # pragma: no cover - spaCy unavailable
    _NLP = None


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
    """Parse generated answer documents (DOCX/text) into structured Q/A slots."""

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
        try:
            suffix = Path(path).suffix.lower()
            if suffix == ".docx":
                return self._parse_docx(path)
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
            return self._parse_text(text, source_name=file_name or Path(path).name)
        finally:
            if cleanup:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def to_responsive_payload(
        self, records: Sequence[QARecord]
    ) -> List[Dict[str, object]]:
        """Convert parsed records into the Responsive upload schema."""
        payload: List[Dict[str, object]] = []
        for record in records:
            try:
                payload.append(record.to_responsive_payload())
            except ValueError:
                continue
        return payload

    # ── DOCX parsing ------------------------------------------------------

    def _parse_docx(self, path: str) -> List[QARecord]:
        """Parse DOCX tables (and paragraph-form Q/A) into QARecord objects.

        Args:
            path: Filesystem path to the DOCX artifact.

        Returns:
            List of QARecord entries extracted from tables/paragraphs.
        """
        if Document is None:
            raise RuntimeError(
                "python-docx is required to parse DOCX answer libraries."
            )
        doc = Document(path)
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

        current_section: Optional[str] = None
        for block in self._iter_docx_blocks(doc):
            if block["type"] == "heading":
                current_section = block["text"]
                flush_pending()
                continue

            if block["type"] == "paragraph":
                text = block["text"]
                if not text:
                    continue
                if self._looks_like_question(text):
                    flush_pending()
                    pending_question = self._strip_question_prefix(text)
                    pending_meta = {
                        "section": current_section,
                        "source": Path(path).name,
                    }
                    pending_lines = []
                elif pending_question:
                    pending_lines.append(text)
                continue

            if block["type"] == "table":
                flush_pending()
                table_records = self._parse_docx_table(
                    block["table"], section=current_section, source=Path(path).name
                )
                records.extend(table_records)

        flush_pending()
        return records

    def _iter_docx_blocks(self, doc) -> Iterable[Dict[str, object]]:
        """Yield docx blocks in reading order with normalized text.

        Args:
            doc: python-docx Document instance.

        Yields:
            Dicts describing each block: {"type": "paragraph"/"heading"/"table", ...}.
        """
        if _DOCX_CTP is None or _DOCX_CTTBL is None or _DOCX_PARAGRAPH is None:
            return
        parent = doc.element.body
        for child in parent.iterchildren():
            if isinstance(child, _DOCX_CTP):
                paragraph = _DOCX_PARAGRAPH(child, doc)
                text = paragraph.text.strip()
                style = (
                    paragraph.style.name.lower()
                    if paragraph.style and paragraph.style.name
                    else ""
                )
                block_type = "heading" if style.startswith("heading") else "paragraph"
                yield {"type": block_type, "text": text}
            elif isinstance(child, _DOCX_CTTBL):
                yield {
                    "type": "table",
                    "table": _DOCX_TABLE(child, doc) if _DOCX_TABLE else None,
                }

    def _parse_docx_table(
        self,
        table: Optional[_DOCX_TABLE],
        *,
        section: Optional[str],
        source: str,
    ) -> List[QARecord]:
        """Parse a DOCX table into QARecord entries when possible.

        Args:
            table: python-docx Table instance (or None if docx is unavailable).
            section: Section/heading context for metadata.
            source: Source filename for provenance.

        Returns:
            List of QARecord entries extracted from the table.
        """
        if table is None:
            return []
        records: List[QARecord] = []
        column_count = len(table.columns)
        if not table.rows:
            return records
        heading_row = table.rows[0]
        heading_text = [cell.text.strip().lower() for cell in heading_row.cells]

        question_col = None
        answer_col = None
        if column_count >= 2:
            for idx, text in enumerate(heading_text):
                if "question" in text and question_col is None:
                    question_col = idx
                if "answer" in text and answer_col is None:
                    answer_col = idx
            if question_col is None or answer_col is None:
                question_col, answer_col = 0, 1 if column_count > 1 else (None, None)

        if question_col is None or answer_col is None:
            return records

        for row_idx, row in enumerate(table.rows):
            if row_idx == 0 and (
                "question" in heading_text[question_col]
                or "answer" in heading_text[answer_col]
            ):
                continue
            question = row.cells[question_col].text.strip()
            answer = row.cells[answer_col].text.strip()
            if not question or not answer:
                continue
            records.append(
                self._build_record(
                    question=question,
                    answers=self._build_answer_variants([answer]),
                    alternate=[],
                    tags=[],
                    source=source,
                    metadata={"section": section},
                )
            )
        return records

    # ── Text parsing ------------------------------------------------------

    def _parse_text(self, text: str, *, source_name: Optional[str]) -> List[QARecord]:
        """Parse plain text into QARecord entries using heuristics.

        Args:
            text: Raw text blob containing questions/answers.
            source_name: Optional provenance label for metadata.

        Returns:
            List of QARecord entries detected in the text.
        """
        records: List[QARecord] = []
        pending_question: Optional[str] = None
        pending_lines: List[str] = []

        def flush() -> None:
            nonlocal pending_question, pending_lines
            if pending_question and pending_lines:
                answer_text = "\n".join(pending_lines).strip()
                if answer_text:
                    records.append(
                        self._build_record(
                            question=pending_question,
                            answers=self._build_answer_variants([answer_text]),
                            alternate=[],
                            tags=[],
                            source=source_name,
                            metadata={},
                        )
                    )
            pending_question = None
            pending_lines = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if self._looks_like_question(line):
                flush()
                pending_question = self._strip_question_prefix(line)
                pending_lines = []
            elif pending_question:
                pending_lines.append(line)
        flush()
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
        """Construct a QARecord while stripping whitespace and empty values."""
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

        if isinstance(source, bytes):
            data = source
        elif hasattr(source, "read"):
            data = source.read()
        else:
            raise TypeError("ApprovedQAParser expects a path, bytes, or binary stream.")
        if not data:
            raise ValueError("Approved QA source is empty.")

        suffix = Path(file_name or "").suffix or ".tmp"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(data)
            temp_path = tmp.name
        return temp_path, True

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        """Heuristic check for whether a paragraph resembles a question prompt."""
        cleaned = text.strip()
        if not cleaned:
            return False
        if cleaned.endswith("?"):
            return True
        if QUESTION_PREFIX_RE.match(cleaned):
            return True
        if NUMBERED_PREFIX_RE.match(cleaned) and _spacy_is_question(cleaned):
            return True
        if _spacy_is_question(cleaned):
            return True
        return False

    @staticmethod
    def _strip_question_prefix(text: str) -> str:
        """Remove 'Question:'-style prefixes when building normalized text."""
        cleaned = text.strip()
        cleaned = QUESTION_PREFIX_RE.sub("", cleaned).strip()
        cleaned = NUMBERED_PREFIX_RE.sub("", cleaned).strip()
        return cleaned


def _spacy_is_question(text: str) -> bool:
    """Use spaCy to detect interrogative/imperative sentences similar to QuestionExtractor."""
    if _NLP is None:
        return False
    doc = _NLP(text)
    sentences = list(doc.sents) if doc.has_annotation("SENT_START") else [doc]
    for sent in sentences:
        sent_text = sent.text.strip()
        if not sent_text:
            continue
        if sent_text.endswith("?"):
            return True
        if any(tok.lower_ in QUESTION_WORDS for tok in sent):
            return True
        root = sent.root
        if "Imp" in root.morph.get("Mood"):
            return True
        first = sent[0]
        if root.tag_ == "VB" and first is root:
            return True
    return False


__all__ = ["ApprovedQAParser", "QARecord", "AnswerVariant"]


# Example usage:
# if __name__ == "__main__":
#     parser = ApprovedQAParser()
#     records = parser.parse("path/to/approved_document.docx")
#     payload = parser.to_responsive_payload(records)
#     print(f"Parsed {len(records)} QA pairs; first entry: {payload[0] if payload else 'N/A'}")
