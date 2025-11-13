"""Parse approved RFP/DDQ answer documents into normalized Q/A records."""

from __future__ import annotations

# Manage filesystem cleanup because parsing may create extra temp files.
import os
import json
# Strip prefixes with regexes so heuristic detection stays fast and expressive.
import re
# Materialize in-memory documents as temp files so python-docx can open them.
import tempfile
# Represent Q/A structures with dataclasses for clarity and type safety.
from dataclasses import dataclass, field
# Normalize file handling across OSes when inferring suffixes or names.
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

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

# Prompt utilities so we can reuse slot_finder's LLM cues when available.
try:  # pragma: no cover - optional dependency
    from backend.prompts import read_prompt as _read_prompt
except ModuleNotFoundError:  # pragma: no cover
    _read_prompt = None  # type: ignore[assignment]

# Optional default LLM client so question detection can leverage the same infra as slot_finder.
try:  # pragma: no cover - optional dependency
    from backend.llm.completions_client import CompletionsClient as _COMPLETIONS_CLIENT
except ModuleNotFoundError:  # pragma: no cover
    _COMPLETIONS_CLIENT = None  # type: ignore[assignment]

# Match literal "Question 3:" style intros so we can normalize noisy prompts.
QUESTION_PREFIX_RE = re.compile(
    r"^(question\s*\d+[:.\-]|\bq[:.\-])\s*", re.IGNORECASE
)
# Match numbered lists like "1." or "a)" to strip outline markers while parsing.
NUMBERED_PREFIX_RE = re.compile(
    r"^(\d+[\).\s]+|[a-z][\).\s]+)", re.IGNORECASE
)
# Broader enumeration matcher used to mimic slot_finder outline stripping.
ENUM_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:\(?\d+(?:\.\d+)*\)?[.)]?)|"  # 1   1.1   2.3.4   (1)   1)
    r"(?:[A-Za-z][.)])|"                   # a)   A)   a.   A.
    r"(?:\([A-Za-z0-9]+\))"               # (a)  (A)  (i)  (1)
    r")\s+",
)
# Common request cues reused from slot_finder so imperative prompts get caught.
QUESTION_PHRASES = (
    "please describe",
    "please provide",
    "explain",
    "detail",
    "outline",
    "how do you",
    "how will you",
    "what is your",
    "what are your",
    "do you",
    "can you",
    "does your",
    "have you",
    "who",
    "when",
    "where",
    "why",
    "which",
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

PSEUDO_HEADING_SECTION_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*)?\s*section\s+\d+", re.IGNORECASE
)
PSEUDO_HEADING_NUMBERED_TITLE_RE = re.compile(
    r"^\s*\d+(?:\.\d+)+\s+[A-Z]", re.IGNORECASE
)
QUESTION_COL_HEADER_RE = re.compile(r"^q(uestion)?\b", re.IGNORECASE)
ANSWER_COL_HEADER_RE = re.compile(r"^a(nswer)?\b", re.IGNORECASE)
ENUM_PREFIX_CUE_WORD_LIMIT = 15


def _default_llm_client() -> Optional[object]:
    """Instantiate the shared CompletionsClient so LLM detection is on by default."""
    if _COMPLETIONS_CLIENT is None:
        print(
            "[ApprovedQAParser] Default LLM client unavailable (CompletionsClient import failed)."
        )
        return None
    model = (
        os.environ.get("APPROVED_QA_PARSER_LLM_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-5-nano"
    )
    try:
        print(
            f"[ApprovedQAParser] Default LLM client enabled using model {model!r}."
        )
        return _COMPLETIONS_CLIENT(model=model)
    except Exception as exc:  # pragma: no cover - depends on local env
        print(
            "[ApprovedQAParser] Failed to initialize default LLM client; "
            f"falling back to heuristics: {exc}"
        )
        return None


def _looks_like_heading_candidate(text: str) -> bool:
    """Return True when a paragraph is likely a heading despite lacking style."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if PSEUDO_HEADING_SECTION_RE.match(stripped):
        return True
    if (
        PSEUDO_HEADING_NUMBERED_TITLE_RE.match(stripped)
        and "?" not in stripped
        and not _quick_question_candidate(stripped)
    ):
        return True
    return False


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
            print(
                "[QARecord] Cannot serialize record because question or answers are missing."
            )
            raise ValueError(
                "QARecord requires both question and answer text to serialize."
            )
        print(
            f"[QARecord] Serializing question {self.question[:80]!r} "
            f"with {len(self.answers)} answer variant(s)."
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
        llm_client: Optional[object] = None,
        llm_chunk_size: int = 40,
    ) -> None:
        """Configure parser defaults for answer labels and language metadata.

        Args:
            default_answer_key (str): Base string used when naming answer variants.
            default_language (str): ISO language code applied to new answer variants.
            llm_client (Optional[object]): Client implementing get_completion used to
                classify ambiguous DOCX blocks with an LLM. When omitted, the parser
                now instantiates backend.llm.CompletionsClient automatically (when
                available) so LLM detection runs by default.
            llm_chunk_size (int): Number of DOCX blocks to include per LLM prompt.
        """
        # Provide a predictable label even if callers pass blanks.
        self.default_answer_key = (
            default_answer_key.strip() or "Answer"
        )
        # Ensure every AnswerVariant carries a language.
        self.default_language = (
            default_language.strip() or "en"
        )
        print(
            "[ApprovedQAParser] Initialized parser "
            f"(default_answer_key={self.default_answer_key!r}, "
            f"default_language={self.default_language!r})."
        )
        # Optional LLM plumbing mirrors slot_finder's question-detection stack.
        if llm_client is not None:
            print("[ApprovedQAParser] Using caller-provided LLM client.")
            self.llm_client = llm_client
        else:
            self.llm_client = _default_llm_client()
        if self.llm_client is None:
            print(
                "[ApprovedQAParser] Default LLM client unavailable; falling back to heuristics."
            )
        self.llm_chunk_size = max(1, int(llm_chunk_size))
        self.enable_llm_detection = True
        self._docx_detect_prompt = (
            (_read_prompt("docx_detect_questions") if _read_prompt else "")
        ).strip()
        self._llm_block_char_limit = 800

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
        print(
            f"[ApprovedQAParser] Starting parse (file_name={file_name!r}, type={type(source).__name__})."
        )
        # Convert streams/bytes into a real file because python-docx and Path APIs need filenames.
        path, cleanup = self._materialize(source, file_name=file_name)
        try:
            # Route to parser-specific code paths using the file suffix to avoid expensive sniffing.
            suffix = Path(path).suffix.lower()
            print(f"[ApprovedQAParser] Materialized path={path}, suffix={suffix or 'n/a'}.")
            if suffix == ".docx":
                # Use the DOCX parser so we can pull Q/A pairs out of tables and headings.
                records = self._parse_docx(path)
            else:
                # Parse everything else as plain text to keep behavior predictable.
                text = Path(path).read_text(encoding="utf-8", errors="ignore")
                # Heuristic paragraph parsing handles adhoc copy/paste exports.
                records = self._parse_text(text, source_name=file_name or Path(path).name)
            records = self._dedupe_records(records)
            print(
                f"[ApprovedQAParser] Finished parse for {file_name or path}; produced {len(records)} record(s)."
            )
            return records
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
        print(
            f"[ApprovedQAParser] Converting {len(records)} record(s) into API payload(s)."
        )
        for record in records:
            try:
                # Ignore malformed records silently.
                payload.append(
                    record.to_responsive_payload()
                )
            except ValueError:
                print(
                    "[ApprovedQAParser] Skipping record during serialization because it raised ValueError."
                )
                continue
        print(
            f"[ApprovedQAParser] Generated {len(payload)} payload(s) ready for upload."
        )
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
        print(f"[ApprovedQAParser] _parse_docx: analyzing {path}.")
        blocks = list(self._iter_docx_blocks(doc))
        block_texts = [self._block_text(block) for block in blocks]
        llm_question_indices = self._llm_detect_questions(blocks, block_texts)
        print(
            "[ApprovedQAParser] DOCX inspection summary: "
            f"{len(blocks)} block(s), {len(llm_question_indices)} LLM-flagged question candidate(s)."
        )

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

            Args:
                None

            Returns:
                None
            """
            nonlocal pending_question, pending_lines, pending_meta
            if pending_question and pending_lines:
                # Merge adjacent paragraph runs because Word often splits answers by line.
                answer_text = "\n".join(pending_lines).strip()
                if answer_text:
                    # Persist the paragraph-mode Q/A so transitions to tables/headings don't drop it.
                    print(
                        "[ApprovedQAParser] Finalizing paragraph answer "
                        f"(question={pending_question!r}, section={pending_meta.get('section')!r})."
                    )
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
        for idx, block in enumerate(blocks):
            block_type = block["type"]
            text = block_texts[idx]
            if block_type == "heading":
                # Headings are a natural divider. Finish whatever answer we were building
                # so the next question starts fresh within the new section.
                # Remember the section title.
                current_section = text
                print(f"[ApprovedQAParser] Entering heading: {current_section}")
                # Prevent bleed-over between sections.
                flush_pending()
                continue

            if block_type == "paragraph":
                if not text:
                    # Skip empty paragraphs; they convey no info.
                    continue
                looks_like_question = self._looks_like_question(text)
                if idx in llm_question_indices:
                    looks_like_question = True
                if looks_like_question:
                    # This line looks like a question prompt. Close the previous pair (if any)
                    # and start collecting lines for this new question.
                    flush_pending()
                    pending_question = self._strip_question_prefix(text)
                    pending_meta = {
                        "section": current_section,
                        "source": Path(path).name,
                    }
                    print(
                        "[ApprovedQAParser] Detected paragraph question "
                        f"(section={current_section!r}): {pending_question!r}"
                    )
                    pending_lines = []
                elif pending_question:
                    # Just a regular sentence that belongs to the current answer.
                    pending_lines.append(text)
                continue

            if block_type == "table":
                # Tables usually list a question in one column and an answer in another.
                print(
                    f"[ApprovedQAParser] Processing table block (section={current_section or 'n/a'})."
                )
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
            print(
                "[ApprovedQAParser] _iter_docx_blocks: missing python-docx helpers; no blocks yielded."
            )
            return
        # Walk the raw XML body to preserve order.
        parent = doc.element.body
        print("[ApprovedQAParser] _iter_docx_blocks: traversing document body.")
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
                if block_type == "paragraph" and _looks_like_heading_candidate(text):
                    block_type = "heading"
                # Downstream logic inspects this.
                print(
                    f"[ApprovedQAParser] _iter_docx_blocks: emitting {block_type} text={text[:80]!r}."
                )
                yield {"type": block_type, "text": text}
            elif isinstance(child, _DOCX_CTTBL):
                # Pass tables through untouched so we can inspect every cell later.
                print("[ApprovedQAParser] _iter_docx_blocks: emitting table block.")
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
        print(
            "[ApprovedQAParser] _parse_docx_table: "
            f"section={section!r}, columns={column_count}, rows={len(table.rows)}."
        )
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
                if question_col is None and QUESTION_COL_HEADER_RE.match(text):
                    question_col = idx
                if answer_col is None and ANSWER_COL_HEADER_RE.match(text):
                    answer_col = idx
            if question_col is None or answer_col is None:
                # Default to the first two columns so even unlabeled tables still produce output.
                question_col, answer_col = 0, 1 if column_count > 1 else (None, None)

        if question_col is None or answer_col is None:
            # Without both columns we can't pair prompts with answers, so skip the table.
            print("[ApprovedQAParser] _parse_docx_table: unable to identify question/answer columns; skipping table.")
            return records

        for row_idx, row in enumerate(table.rows):
            if row_idx == 0 and (
                QUESTION_COL_HEADER_RE.match(heading_text[question_col])
                or ANSWER_COL_HEADER_RE.match(heading_text[answer_col])
                or "question" in heading_text[question_col]
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
            print(
                "[ApprovedQAParser] Table row -> question/answer pair "
                f"(section={section!r}, question={question[:50]!r})"
            )
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

    def _block_text(self, block: Dict[str, object]) -> str:
        """Return normalized text for a DOCX block (paragraph, heading, or table)."""
        block_type = block.get("type")
        if block_type == "table":
            text = self._table_to_text(block.get("table"))
            print(
                f"[ApprovedQAParser] _block_text: converted table to text snippet {text[:80]!r}."
            )
            return text
        text = str(block.get("text") or "").strip()
        print(
            f"[ApprovedQAParser] _block_text: normalized {block_type} text {text[:80]!r}."
        )
        return text

    def _table_to_text(self, table: Optional[_DOCX_TABLE]) -> str:
        """Flatten a DOCX table into a pipe-delimited string for LLM context."""
        if table is None:
            print("[ApprovedQAParser] _table_to_text: received None table.")
            return ""
        cell_texts: List[str] = []
        try:
            for row in table.rows:
                for cell in row.cells:
                    value = (cell.text or "").strip()
                    if value:
                        cell_texts.append(value)
        except Exception:
            return ""
        flattened = " | ".join(cell_texts)
        print(
            f"[ApprovedQAParser] _table_to_text: flattened table with {len(cell_texts)} cells."
        )
        return flattened

    def _llm_detect_questions(
        self,
        blocks: Sequence[Dict[str, object]],
        block_texts: Sequence[str],
    ) -> Set[int]:
        """Use the shared DOCX detect prompt to flag question-like block indices."""
        if (
            not self.enable_llm_detection
            or not self.llm_client
            or not self._docx_detect_prompt
        ):
            print("[ApprovedQAParser] _llm_detect_questions: LLM detection disabled.")
            return set()
        detected: Set[int] = set()
        serializable: List[Dict[str, object]] = []
        for idx, (block, text) in enumerate(zip(blocks, block_texts)):
            serializable.append(
                {
                    "index": idx,
                    "type": block.get("type", "paragraph"),
                    "text": (text or "")[: self._llm_block_char_limit],
                }
            )
        chunk_size = self.llm_chunk_size
        prompt_template = self._docx_detect_prompt
        for start in range(0, len(serializable), chunk_size):
            chunk = serializable[start : start + chunk_size]
            if not chunk or not any(item["text"] for item in chunk):
                continue
            excerpt = json.dumps({"blocks": chunk}, ensure_ascii=False)
            prompt = prompt_template.format(excerpt=excerpt)
            try:
                print(
                    f"[ApprovedQAParser] _llm_detect_questions: sending chunk "
                    f"{start // chunk_size + 1} containing {len(chunk)} block(s)."
                )
                completion = self.llm_client.get_completion(prompt, json_output=True)
            except Exception:
                print(
                    "[ApprovedQAParser] _llm_detect_questions: LLM call failed; skipping chunk."
                )
                continue
            content: Any
            if isinstance(completion, tuple):
                content = completion[0]
            else:
                content = completion
            try:
                payload = json.loads(content)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            questions = payload.get("questions", [])
            if not isinstance(questions, list):
                continue
            for idx_val in questions:
                try:
                    detected.add(int(idx_val))
                except (TypeError, ValueError):
                    continue
        print(
            f"[ApprovedQAParser] _llm_detect_questions: detected {len(detected)} question index(es)."
        )
        return detected

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
        print(
            f"[ApprovedQAParser] Parsing plain text source={source_name or 'n/a'} with {len(text.splitlines())} line(s)."
        )
        records: List[QARecord] = []
        # Current question candidate.
        pending_question: Optional[str] = None
        # Accumulated answer body.
        pending_lines: List[str] = []

        def flush() -> None:
            """Persist the buffered text as an answer for the pending question.

            Args:
                None

            Returns:
                None
            """
            nonlocal pending_question, pending_lines
            if pending_question and pending_lines:
                # Combine consecutive lines because plain text exports often wrap sentences.
                answer_text = "\n".join(pending_lines).strip()
                if answer_text:
                    print(
                        "[ApprovedQAParser] Finalizing text-mode QA "
                        f"(question={pending_question!r}, source={source_name!r})."
                    )
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
                print(
                    f"[ApprovedQAParser] Detected text question: {pending_question!r}"
                )
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
        print(
            f"[ApprovedQAParser] _build_answer_variants: created {len(variants)} variant(s)."
        )
        return variants

    @classmethod
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
        record = QARecord(
            question=question.strip(),
            answers=answers,
            alternate_questions=[a.strip() for a in alternate if a and str(a).strip()],
            tags=[t.strip() for t in tags if t and str(t).strip()],
            source=source,
            # Drop empty metadata entries to keep payload tidy.
            metadata=cls._normalize_metadata(metadata),
        )
        print(
            "[ApprovedQAParser] _build_record: constructed record "
            f"question={record.question[:80]!r}, answers={len(record.answers)}."
        )
        return record

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
            print(f"[ApprovedQAParser] _materialize: using existing path {path}.")
            return str(path), False

        if isinstance(source, bytes):
            # Raw bytes provided up front, usually from uploads.
            data = source
            print(
                f"[ApprovedQAParser] _materialize: received bytes input ({len(data)} bytes)."
            )
        elif hasattr(source, "read"):
            # Pull the entire stream into memory so we can write a temp file.
            data = source.read()
            print(
                "[ApprovedQAParser] _materialize: read data from stream "
                f"({len(data)} bytes)."
            )
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
        print(
            f"[ApprovedQAParser] _materialize: wrote {len(data)} bytes to temp file {temp_path}."
        )
        return temp_path, True

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        """Determine whether a text fragment resembles a question prompt.

        Args:
            text: Raw paragraph text to evaluate.

        Returns:
            bool: True if the text appears to be a question, False otherwise.
        """
        cleaned = (text or "").strip()
        reason = ""
        if not cleaned:
            reason = "empty string"
            result = False
            print(
                f"[ApprovedQAParser] _looks_like_question: {result} (reason={reason}) text={cleaned[:80]!r}"
            )
            return result

        if _quick_question_candidate(cleaned):
            reason = "quick heuristic"
            print(
                f"[ApprovedQAParser] _looks_like_question: True (reason={reason}) text={cleaned[:80]!r}"
            )
            return True

        is_enum_prefix = bool(ENUM_PREFIX_RE.match(cleaned))
        normalized = _strip_enum_prefix(cleaned).strip()
        lower_norm = normalized.lower()

        if is_enum_prefix:
            word_count = len(normalized.split())
            if "?" in normalized:
                reason = "enumerated question mark"
                print(
                    f"[ApprovedQAParser] _looks_like_question: True (reason={reason}) text={cleaned[:80]!r}"
                )
                return True
            if lower_norm and any(
                phrase in lower_norm for phrase in QUESTION_PHRASES
            ) and word_count <= ENUM_PREFIX_CUE_WORD_LIMIT:
                reason = f"enumerated cue <= {ENUM_PREFIX_CUE_WORD_LIMIT} words"
                print(
                    f"[ApprovedQAParser] _looks_like_question: True (reason={reason}) text={cleaned[:80]!r}"
                )
                return True
            reason = "enumerated without cues"
            print(
                f"[ApprovedQAParser] _looks_like_question: False (reason={reason}) text={cleaned[:80]!r}"
            )
            return False

        if any(lower_norm.startswith(phrase) for phrase in QUESTION_PHRASES):
            reason = "leading cue phrase"
            result = True
        elif any(phrase in lower_norm for phrase in QUESTION_PHRASES):
            reason = "contains cue phrase"
            result = True
        else:
            lowered_clean = cleaned.lower()
            if QUESTION_PREFIX_RE.match(cleaned) or lowered_clean.startswith(
                ("prompt:", "rfp question:")
            ):
                reason = "explicit prefix"
                result = True
            elif _spacy_is_question(cleaned):
                reason = "spaCy heuristic"
                result = True
            else:
                reason = "heuristics failed"
                result = False
        print(
            f"[ApprovedQAParser] _looks_like_question: {result} (reason={reason}) text={cleaned[:80]!r}"
        )
        return result

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
        cleaned = _strip_enum_prefix(cleaned)
        print(
            f"[ApprovedQAParser] _strip_question_prefix: original={text[:80]!r}, cleaned={cleaned[:80]!r}"
        )
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
        print("[ApprovedQAParser] _spacy_is_question: spaCy unavailable; returning False.")
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
            print("[ApprovedQAParser] _spacy_is_question: detected question mark.")
            return True
        if any(tok.lower_ in QUESTION_WORDS for tok in sent):
            # Look for interrogative pronouns to catch sentences lacking question marks.
            print("[ApprovedQAParser] _spacy_is_question: detected interrogative pronoun.")
            return True
        root = sent.root
        if "Imp" in root.morph.get("Mood"):
            # Imperatives like "Describe" usually indicate prompts.
            print("[ApprovedQAParser] _spacy_is_question: imperative mood detected.")
            return True
        first = sent[0]
        if root.tag_ == "VB" and first is root:
            # Plain verb-first sentences (commands) imply a question even without punctuation.
            print("[ApprovedQAParser] _spacy_is_question: verb-first sentence detected.")
            return True
    print("[ApprovedQAParser] _spacy_is_question: no interrogative cues found.")
    return False


def _quick_question_candidate(text: str) -> bool:
    """Fast screening heuristic mirroring slot_finder's first-layer check.

    Args:
        text (str): Candidate sentence or paragraph to inspect.

    Returns:
        bool: True if lightweight keyword checks suggest the text is a question.
    """
    # Normalize incoming text because blank lines and surrounding spaces are common.
    raw = (text or "").strip()
    reason = ""
    if not raw:
        result = False
        reason = "empty"
    else:
        # Lowercase once so phrase checks are case-insensitive.
        lower = raw.lower()
        if "?" in raw:
            result = True
            reason = "contains ?"
        elif any(phrase in lower for phrase in QUESTION_PHRASES):
            # Reuse slot_finder keywords so we stay consistent with upstream heuristics.
            result = True
            reason = "keyword match"
        else:
            result = False
            reason = "no cues"
    print(
        f"[ApprovedQAParser] _quick_question_candidate: {result} (reason={reason}) text={raw[:80]!r}"
    )
    return result


def _strip_enum_prefix(text: str) -> str:
    """Remove leading numbering/outline tokens.

    Args:
        text (str): Text that may begin with identifiers like "1.", "(a)", or "A)".

    Returns:
        str: Text with at most one leading outline token removed.
    """
    if not text:
        print("[ApprovedQAParser] _strip_enum_prefix: received empty text.")
        return ""
    original = text
    cleaned = text
    iterations = 0
    while True:
        stripped_once = ENUM_PREFIX_RE.sub("", cleaned, count=1)
        if stripped_once == cleaned:
            break
        cleaned = stripped_once.strip()
        iterations += 1
    cleaned = cleaned.strip()
    print(
        f"[ApprovedQAParser] _strip_enum_prefix: removed {iterations} prefix(es); "
        f"original={original[:40]!r}, cleaned={cleaned[:40]!r}"
    )
    return cleaned


def _normalize_section_name(section: Optional[str]) -> Optional[str]:
    """Collapse section strings into a canonical lowercase token."""
    if not section:
        return None
    cleaned = re.sub(r"\s+", " ", section).strip()
    if not cleaned:
        return None
    numeric = re.match(r"(\d+(?:\.\d+)*)", cleaned)
    if numeric:
        normalized = numeric.group(1).lower()
    else:
        normalized = cleaned.lower()
    print(
        f"[ApprovedQAParser] _normalize_section_name: original={section[:80]!r}, normalized={normalized!r}"
    )
    return normalized


# Re-export public API so external modules can import the parser without digging through modules.
__all__ = ["ApprovedQAParser", "QARecord", "AnswerVariant"]


# Example usage:
# if __name__ == "__main__":
#     parser = ApprovedQAParser()
#     records = parser.parse("path/to/approved_document.docx")
#     payload = parser.to_responsive_payload(records)
#     print(f"Parsed {len(records)} QA pairs; first entry: {payload[0] if payload else 'N/A'}")
    @staticmethod
    def _normalize_metadata(metadata: Optional[Dict[str, object]]) -> Dict[str, object]:
        """Normalize metadata values (e.g., section names) and drop empty entries."""
        cleaned: Dict[str, object] = {}
        for key, value in (metadata or {}).items():
            if value is None:
                continue
            if key == "section" and isinstance(value, str):
                normalized_section = _normalize_section_name(value)
                if normalized_section:
                    cleaned[key] = normalized_section
                continue
            cleaned[key] = value
        return cleaned


    def _dedupe_records(self, records: Sequence[QARecord]) -> List[QARecord]:
        """Remove duplicate questions while preserving order."""
        seen: Set[str] = set()
        deduped: List[QARecord] = []
        for record in records:
            normalized_key = re.sub(r"\s+", " ", record.question or "").strip().lower()
            if not normalized_key:
                deduped.append(record)
                continue
            if normalized_key in seen:
                print(
                    f"[ApprovedQAParser] Dedupe: dropping duplicate question {record.question[:80]!r}."
                )
                continue
            seen.add(normalized_key)
            deduped.append(record)
        return deduped
