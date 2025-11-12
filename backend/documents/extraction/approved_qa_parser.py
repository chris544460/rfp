"""Parse approved RFP/DDQ answer documents into normalized Q/A records."""

from __future__ import annotations

# Manage filesystem cleanup because parsing may create extra temp files.
import os
# Strip prefixes with regexes so heuristic detection stays fast and expressive.
import re
# Materialize in-memory documents as temp files so python-docx can open them.
import tempfile
# Represent Q/A structures with dataclasses for clarity and type safety.
from dataclasses import dataclass, field
# Normalize file handling across OSes when inferring suffixes or names.
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple, Union

# Optional dependency; needed only when users upload DOCX files.
try:
    # Import lazily so environments without python-docx can still use text parsing paths.
    from docx import Document as _DOCX_DOCUMENT
    # Table wrapper to traverse structured question/answer grids.
    from docx.table import Table as _DOCX_TABLE
    # Paragraph wrapper to inspect headings and body text.
    from docx.text.paragraph import Paragraph as _DOCX_PARAGRAPH
    # Low-level paragraph node needed to iterate blocks sequentially.
    from docx.oxml.text.paragraph import CT_P as _DOCX_CTP
    # Low-level table node to detect tables without losing ordering.
    from docx.oxml.table import CT_Tbl as _DOCX_CTTBL
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    # Flag absence so parser can error cleanly and fall back to text mode.
    _DOCX_DOCUMENT = None  # type: ignore[assignment]
    _DOCX_TABLE = None  # type: ignore[assignment]
    _DOCX_PARAGRAPH = None  # type: ignore[assignment]
    _DOCX_CTP = None  # type: ignore[assignment]
    _DOCX_CTTBL = None  # type: ignore[assignment]

# Optional spaCy dependency so we can lean on NLP heuristics when regexes are insufficient.
try:
    # spaCy handles question/statement heuristics more accurately than regexes alone.
    import spacy as _SPACY
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    # Fall back to regex-only behavior when spaCy is missing so installs stay lightweight.
    _SPACY = None

# Match literal "Question 3:" style intros so we can normalize noisy prompts.
QUESTION_PREFIX_RE = re.compile(
    r"^(question\s*\d+[:.\-]|\bq[:.\-])\s*", re.IGNORECASE
)
# Match numbered lists like "1." or "a)" to strip outline markers while parsing.
NUMBERED_PREFIX_RE = re.compile(
    r"^(\d+[\).\s]+|[a-z][\).\s]+)", re.IGNORECASE
)
# Core interrogative anchors to help spaCy decide whether imperative sentences are question-like.
QUESTION_WORDS = {
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "which",
}

if _SPACY is not None:  # pragma: no cover - exercised via runtime
    try:
        # Prefer the standard English pipeline because it includes POS/morph features used below.
        _NLP = _SPACY.load("en_core_web_sm")
    except (OSError, IOError):
        # Fall back to a blank model so deployments without models still detect questions crudely.
        _NLP = _SPACY.blank("en")
        if "sentencizer" not in _NLP.pipe_names:
            # Add a sentencizer so we can iterate sentences even in the blank model.
            _NLP.add_pipe("sentencizer")
else:  # pragma: no cover - spaCy unavailable
    # Signal to other helpers to skip spaCy-specific logic to avoid crashes.
    _NLP = None


@dataclass
class AnswerVariant:
    """Single answer option attached to a question.

    Attributes:
        key (str): Stable identifier used by consuming platforms (e.g., "Answer").
        value (str): Human-readable answer text captured from the source file.
        is_primary (bool): Whether the value is the canonical/primary response.
        language_code (str): ISO language code required by the Responsive API.
    """

    # Unique label shown in client systems (e.g., "Answer") so uploads stay deterministic.
    key: str
    # Actual text extracted from the document so we can store the human-readable response.
    value: str
    # Flag the main answer vs alternates because Responsive surfaces the primary value first.
    is_primary: bool = True
    # Language metadata for API compatibility when multi-lingual libraries get synced.
    language_code: str = "en"


@dataclass
class QARecord:
    """Normalized representation of a question plus associated answers.

    Attributes:
        question (str): Canonical question text used for deduplication.
        answers (List[AnswerVariant]): Primary + alternate answer variants.
        alternate_questions (List[str]): Alternate phrasings sourced from the file.
        tags (List[str]): Simple metadata tags that aid search/filtering.
        source (Optional[str]): File name that produced this record.
        metadata (Dict[str, object]): Misc structured data such as section names.

    Methods:
        to_responsive_payload: Convert the record into Responsive's schema.
    """

    # Canonical question text so deduplication routines have a stable key.
    question: str
    # Ordered answer options because some exporters expect deterministic variant order.
    answers: List[AnswerVariant] = field(default_factory=list)
    # Extra phrasings to capture synonyms that often appear in RFPs.
    alternate_questions: List[str] = field(default_factory=list)
    # Arbitrary labels (topics, etc.) so search features can filter records.
    tags: List[str] = field(default_factory=list)
    # File name (if available) to trace the record back to its source.
    source: Optional[str] = None
    # Loose extras (section) for UI display or analytics.
    metadata: Dict[str, object] = field(default_factory=dict)

    def to_responsive_payload(self) -> Dict[str, object]:
        """Convert this QARecord into the Responsive upload schema.

        Returns:
            Dict[str, object]: Payload compatible with POST /answer-lib/add.

        Raises:
            ValueError: If the record is missing either question text or any
                non-empty answers after filtering.
        """
        if not self.question or not self.answers:
            raise ValueError(
                "QARecord requires both question and answer text to serialize."
            )
        payload = {
            # Keep the normalized prompt so Responsive can match duplicates reliably.
            "question": self.question,
            # Copy alternates verbatim because the API expects the original phrasings.
            "alternateQuestions": list(self.alternate_questions),
            "answers": [
                {
                    "key": ans.key,
                    "value": ans.value,
                    "isPrimary": ans.is_primary,
                    "languageCode": ans.language_code,
                }
                for ans in self.answers
                # Drop empty answer bodies to avoid API errors that reject blank strings.
                if ans.value
            ],
            # Preserve tags so filtering/search remains intact.
            "tags": list(self.tags),
        }
        if not payload["answers"]:
            raise ValueError(
                "QARecord cannot serialize when answers are empty after filtering."
            )
        return payload


class ApprovedQAParser:
    """Parse generated answer documents (DOCX/text) into structured Q/A slots.

    Attributes:
        default_answer_key (str): Label applied to the first answer variant when
            building payloads (subsequent variants get suffixed forms).
        default_language (str): ISO language code used for newly created answers.

    Methods:
        parse: Detect the proper backend (DOCX vs. text) and return QA records.
        to_responsive_payload: Convert parsed QA records into API payloads.
    """

    def __init__(
        self,
        *,
        default_answer_key: str = "Answer",
        default_language: str = "en",
    ) -> None:
        """Configure parser defaults for answer labels and language metadata.

        Args:
            default_answer_key (str): Base string used when naming answer variants.
            default_language (str): ISO language code applied to new answer variants.
        """
        # Provide a predictable label even if callers pass blanks.
        self.default_answer_key = (
            default_answer_key.strip() or "Answer"
        )
        # Ensure every AnswerVariant carries a language.
        self.default_language = (
            default_language.strip() or "en"
        )

    def parse(
        self,
        source: Union[str, Path, BinaryIO, bytes],
        *,
        file_name: Optional[str] = None,
    ) -> List[QARecord]:
        """Materialize the input file and route it to the appropriate parser.

        Args:
            source (Union[str, Path, BinaryIO, bytes]): Path, file-like object,
                or raw bytes containing the document contents.
            file_name (Optional[str]): Optional hint used to infer a suffix for
                temp files when the source is a stream or bytes.

        Returns:
            List[QARecord]: Ordered QA pairs extracted from the document.

        Raises:
            FileNotFoundError: If a provided filesystem path does not exist.
            RuntimeError: If DOCX parsing is requested without python-docx.
            ValueError: If the provided data stream is empty.
        """
        # Convert streams/bytes into a real file because python-docx and Path APIs need filenames.
        path, cleanup = self._materialize(source, file_name=file_name)
        try:
            # Route to parser-specific code paths using the file suffix to avoid expensive sniffing.
            suffix = Path(path).suffix.lower()
            if suffix == ".docx":
                # Use the DOCX parser so we can pull Q/A pairs out of tables and headings.
                return self._parse_docx(path)
            # Parse everything else as plain text to keep behavior predictable.
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
            # Heuristic paragraph parsing handles adhoc copy/paste exports.
            return self._parse_text(text, source_name=file_name or Path(path).name)
        finally:
            if cleanup:
                try:
                    # Delete temp files we created so repeated parsing does not leak disk space.
                    os.unlink(path)
                except OSError:
                    # Ignore cleanup failures; they are non-fatal and usually OS-level races.
                    pass

    def to_responsive_payload(
        self, records: Sequence[QARecord]
    ) -> List[Dict[str, object]]:
        """Convert parsed records into the Responsive upload schema.

        Args:
            records (Sequence[QARecord]): QARecord instances to serialize.

        Returns:
            List[Dict[str, object]]: API-ready payloads for every valid record.
        """
        # Collect successfully serialized entries.
        payload: List[Dict[str, object]] = []
        for record in records:
            try:
                # Ignore malformed records silently.
                payload.append(
                    record.to_responsive_payload()
                )
            except ValueError:
                continue
        return payload

    # ── DOCX parsing ------------------------------------------------------

    def _parse_docx(self, path: str) -> List[QARecord]:
        """Parse DOCX tables (and paragraph-form Q/A) into QARecord objects.

        Args:
            path (str): Filesystem path to the DOCX file to parse.

        Returns:
            List[QARecord]: Entries extracted from tables and paragraphs.
        """
        if _DOCX_DOCUMENT is None:
            raise RuntimeError(
                "python-docx is required to parse DOCX answer libraries."
            )
        # Load the DOCX structure once so we can walk paragraphs and tables without re-reading disk.
        doc = _DOCX_DOCUMENT(path)
        # Collect normalized records in document order to preserve context.
        records: List[QARecord] = []

        # Track the current paragraph question because paragraphs may interleave with tables.
        pending_question: Optional[str] = None
        # Accumulate multi-line answers until we hit the next question delimiter.
        pending_lines: List[str] = []
        # Store section/source metadata so we can apply headings to paragraph answers.
        pending_meta: Dict[str, object] = {}

        def flush_pending() -> None:
            """Persist the currently buffered paragraph Q/A pair if possible.

            Returns:
                None
            """
            nonlocal pending_question, pending_lines, pending_meta
            if pending_question and pending_lines:
                # Merge adjacent paragraph runs because Word often splits answers by line.
                answer_text = "\n".join(pending_lines).strip()
                if answer_text:
                    # Persist the paragraph-mode Q/A so transitions to tables/headings don't drop it.
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
            # Reset buffers so the next heading/question starts from a clean slate.
            pending_question = None
            pending_lines = []
            pending_meta = {}

        # Track headings for metadata so we can attribute each QA pair to its section later.
        current_section: Optional[str] = None
        for block in self._iter_docx_blocks(doc):
            if block["type"] == "heading":
                # Headings are a natural divider. Finish whatever answer we were building
                # so the next question starts fresh within the new section.
                # Remember the section title.
                current_section = block["text"]
                # Prevent bleed-over between sections.
                flush_pending()
                continue

            if block["type"] == "paragraph":
                text = block["text"]
                if not text:
                    # Skip empty paragraphs; they convey no info.
                    continue
                if self._looks_like_question(text):
                    # This line looks like a question prompt. Close the previous pair (if any)
                    # and start collecting lines for this new question.
                    flush_pending()
                    pending_question = self._strip_question_prefix(text)
                    pending_meta = {
                        "section": current_section,
                        "source": Path(path).name,
                    }
                    pending_lines = []
                elif pending_question:
                    # Just a regular sentence that belongs to the current answer.
                    pending_lines.append(text)
                continue

            if block["type"] == "table":
                # Tables usually list a question in one column and an answer in another.
                flush_pending()
                table_records = self._parse_docx_table(
                    block["table"], section=current_section, source=Path(path).name
                )
                # Merge table-derived rows so table content sits alongside paragraph answers.
                records.extend(table_records)

        # Persist any trailing paragraph answer once the loop ends.
        flush_pending()
        return records

    def _iter_docx_blocks(self, doc) -> Iterable[Dict[str, object]]:
        """Yield docx blocks in reading order with normalized text.

        Args:
            doc (_DOCX_DOCUMENT): python-docx Document instance.

        Yields:
            Dict[str, object]: Description of each block, e.g. {"type": "paragraph"}.
        """
        if _DOCX_CTP is None or _DOCX_CTTBL is None or _DOCX_PARAGRAPH is None:
            return
        # Walk the raw XML body to preserve order.
        parent = doc.element.body
        for child in parent.iterchildren():
            if isinstance(child, _DOCX_CTP):
                # Turn each paragraph into a lightweight dict with its text and whether
                # Word styled it as a heading.
                # Wrap node for convenience.
                paragraph = _DOCX_PARAGRAPH(child, doc)
                text = paragraph.text.strip()
                style = (
                    paragraph.style.name.lower()
                    if paragraph.style and paragraph.style.name
                    else ""
                )
                # Guard against missing style info.
                block_type = "heading" if style.startswith("heading") else "paragraph"
                # Downstream logic inspects this.
                yield {"type": block_type, "text": text}
            elif isinstance(child, _DOCX_CTTBL):
                # Pass tables through untouched so we can inspect every cell later.
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
            table (Optional[_DOCX_TABLE]): python-docx Table instance (or None if unavailable).
            section (Optional[str]): Heading text associated with the current table.
            source (str): File name used to label the resulting records.

        Returns:
            List[QARecord]: Records extracted from each valid table row.
        """
        if table is None:
            return []
        # Accumulate records per row.
        records: List[QARecord] = []
        # Keep note for fallback heuristics.
        column_count = len(table.columns)
        if not table.rows:
            # Empty table -> nothing to parse.
            return records
        # Inspect the first row for column labels so we can adapt to custom column ordering.
        heading_row = table.rows[0]
        heading_text = [cell.text.strip().lower() for cell in heading_row.cells]

        question_col = None
        answer_col = None
        if column_count >= 2:
            for idx, text in enumerate(heading_text):
                if "question" in text and question_col is None:
                    # Remember the first matching column.
                    question_col = idx
                if "answer" in text and answer_col is None:
                    answer_col = idx
            if question_col is None or answer_col is None:
                # Default to the first two columns so even unlabeled tables still produce output.
                question_col, answer_col = 0, 1 if column_count > 1 else (None, None)

        if question_col is None or answer_col is None:
            # Without both columns we can't pair prompts with answers, so skip the table.
            return records

        for row_idx, row in enumerate(table.rows):
            if row_idx == 0 and (
                "question" in heading_text[question_col]
                or "answer" in heading_text[answer_col]
            ):
                # Skip header row when it clearly labels columns.
                continue
            # Extract the prompt text.
            question = row.cells[question_col].text.strip()
            # Extract the paired answer.
            answer = row.cells[answer_col].text.strip()
            if not question or not answer:
                # Ignore incomplete entries to avoid junk data.
                continue
            records.append(
                # Store each table row as an independent record so ordering matches the document.
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
            text (str): Raw text blob containing questions and answers.
            source_name (Optional[str]): Optional label indicating which file the
                text came from.

        Returns:
            List[QARecord]: QA pairs detected via heuristic parsing.
        """
        # Parsed output in document order.
        records: List[QARecord] = []
        # Current question candidate.
        pending_question: Optional[str] = None
        # Accumulated answer body.
        pending_lines: List[str] = []

        def flush() -> None:
            """Persist the buffered text as an answer for the pending question.

            Returns:
                None
            """
            nonlocal pending_question, pending_lines
            if pending_question and pending_lines:
                # Combine consecutive lines because plain text exports often wrap sentences.
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
                        ),
                    )
            # Reset trackers so the next detected question starts fresh.
            pending_question = None
            pending_lines = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                # Skip blank lines—they usually mean paragraph spacing.
                continue
            if self._looks_like_question(line):
                # Finish the previous QA pair before starting a new one.
                flush()
                pending_question = self._strip_question_prefix(line)
                pending_lines = []
            elif pending_question:
                # Treat contiguous lines as the answer.
                pending_lines.append(line)
        # Commit whichever question was last seen so trailing answers survive EOF.
        flush()
        return records

    # ── Helpers -----------------------------------------------------------

    def _build_answer_variants(self, values: Iterable[str]) -> List[AnswerVariant]:
        """Normalize raw strings into AnswerVariant entries with default keys.

        Args:
            values (Iterable[str]): Answer strings gathered from the source.

        Returns:
            List[AnswerVariant]: Variants in the order they were discovered,
                with the first non-empty value marked as primary.
        """
        # Preserve input ordering.
        variants: List[AnswerVariant] = []
        for idx, raw_value in enumerate(values):
            text = (raw_value or "").strip()
            if not text:
                # Ignore empty strings to avoid blank records.
                continue
            # Label alternates uniquely so APIs like Responsive can distinguish them.
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
            question (str): Canonical question text as extracted from the source.
            answers (List[AnswerVariant]): Prepared variants tied to the question.
            alternate (Sequence[str]): Additional phrasings captured for the question.
            tags (Sequence[str]): Metadata tags to attach to the record.
            source (Optional[str]): File name or label describing the source.
            metadata (Optional[Dict[str, object]]): Structured metadata such as sections.

        Returns:
            QARecord: Cleaned record with empty fields removed.
        """
        return QARecord(
            question=question.strip(),
            answers=answers,
            alternate_questions=[a.strip() for a in alternate if a and str(a).strip()],
            tags=[t.strip() for t in tags if t and str(t).strip()],
            source=source,
            # Drop empty metadata entries to keep payload tidy.
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
            source (Union[str, Path, BinaryIO, bytes]): Path, bytes, or binary stream
                containing the document.
            file_name (Optional[str]): Optional hint for deriving a suffix.

        Returns:
            Tuple[str, bool]: (path, cleanup) where cleanup indicates the caller
            should delete the temp file after use.

        Raises:
            FileNotFoundError: When the provided path does not exist.
            TypeError: If the stream type is unsupported.
            ValueError: When the data is empty.
        """
        if isinstance(source, (str, Path)):
            path = Path(source)
            if not path.exists():
                raise FileNotFoundError(f"Approved QA source '{path}' does not exist.")
            # Already on disk; no cleanup required because caller controls lifecycle.
            return str(path), False

        if isinstance(source, bytes):
            # Raw bytes provided up front, usually from uploads.
            data = source
        elif hasattr(source, "read"):
            # Pull the entire stream into memory so we can write a temp file.
            data = source.read()
        else:
            raise TypeError("ApprovedQAParser expects a path, bytes, or binary stream.")
        if not data:
            raise ValueError("Approved QA source is empty.")

        # Preserve helpful suffixes so other parsers infer format correctly.
        suffix = Path(file_name or "").suffix or ".tmp"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(data)
            # Keep path for the caller to consume.
            temp_path = tmp.name
        # Signal that caller must delete the temp file to avoid leaking disk.
        return temp_path, True

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        """Determine whether a text fragment resembles a question prompt.

        Args:
            text: Raw paragraph text to evaluate.

        Returns:
            bool: True if the text appears to be a question, False otherwise.
        """
        cleaned = text.strip()
        if not cleaned:
            # Empty strings can't encode questions and just add noise.
            return False
        if cleaned.endswith("?"):
            # Literal question mark is the strongest indicator.
            return True
        if QUESTION_PREFIX_RE.match(cleaned):
            # Explicit "Question:" cue means the author annotated prompts.
            return True
        if NUMBERED_PREFIX_RE.match(cleaned) and _spacy_is_question(cleaned):
            # Numbered lists need NLP confirmation so plain outlines do not trigger.
            return True
        if _spacy_is_question(cleaned):
            # Fall back to NLP heuristics for imperatives like "Describe...".
            return True
        return False

    @staticmethod
    def _strip_question_prefix(text: str) -> str:
        """Remove numbering/prefix cruft from question prompts.

        Args:
            text: Question text that may contain prefixes (e.g., 'Q1:', '1.').

        Returns:
            str: Cleaned question text without leftover numbering noise.
        """
        cleaned = text.strip()
        cleaned = QUESTION_PREFIX_RE.sub("", cleaned).strip()
        cleaned = NUMBERED_PREFIX_RE.sub("", cleaned).strip()
        # Return the bare prompt without numbering cruft to keep canonical questions consistent.
        return cleaned


def _spacy_is_question(text: str) -> bool:
    """Use spaCy to detect interrogative/imperative sentences.

    Args:
        text: Candidate sentence or paragraph requiring classification.

    Returns:
        bool: True if spaCy signals that the text is question-like.
    """
    if _NLP is None:
        # spaCy unavailable; stick to regex heuristics so we do not raise at runtime.
        return False
    # Run the lightweight pipeline on the candidate text.
    doc = _NLP(text)
    # Handle models without sentence boundaries by treating the doc as one sentence.
    sentences = (
        list(doc.sents) if doc.has_annotation("SENT_START") else [doc]
    )
    for sent in sentences:
        sent_text = sent.text.strip()
        if not sent_text:
            continue
        if sent_text.endswith("?"):
            return True
        if any(tok.lower_ in QUESTION_WORDS for tok in sent):
            # Look for interrogative pronouns to catch sentences lacking question marks.
            return True
        root = sent.root
        if "Imp" in root.morph.get("Mood"):
            # Imperatives like "Describe" usually indicate prompts.
            return True
        first = sent[0]
        if root.tag_ == "VB" and first is root:
            # Plain verb-first sentences (commands) imply a question even without punctuation.
            return True
    return False


# Re-export public API so external modules can import the parser without digging through modules.
__all__ = ["ApprovedQAParser", "QARecord", "AnswerVariant"]


# Example usage:
# if __name__ == "__main__":
#     parser = ApprovedQAParser()
#     records = parser.parse("path/to/approved_document.docx")
#     payload = parser.to_responsive_payload(records)
#     print(f"Parsed {len(records)} QA pairs; first entry: {payload[0] if payload else 'N/A'}")
