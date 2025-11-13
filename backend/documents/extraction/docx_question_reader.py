"""
docx_question_reader.py

Object-oriented helper that mirrors the backend QuestionExtractor logic so DOCX
files can be inspected programmatically or via a simple CLI entry point. In
addition to reporting the detected questions, this module now classifies every
block of text as a question, an answer to the most recent question, or neither,
and explains why each classification was made.

Examples:
    python backend/documents/extraction/docx_question_reader.py path/to/file.docx
    python backend/documents/extraction/docx_question_reader.py file.docx --json
    python backend/documents/extraction/docx_question_reader.py file.docx --show-metadata
    python backend/documents/extraction/docx_question_reader.py file.docx --use-llm-classifier

Use the --treat-docx-as-text flag to force the fallback text-extraction path.
That mode requires the custom CompletionsClient environment variables because
it calls the LLM prompt used in `QuestionExtractor.extract_from_text`.
Similarly, --use-llm-classifier instantiates a CompletionsClient to have the
LLM decide whether each block is a question, an answer, or unrelated.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from backend.documents.docx.slot_finder import (
    QUESTION_PHRASES,
    _expand_doc_blocks,
    _iter_block_items,
)
from backend.documents.extraction.question_extractor import QuestionExtractor
from backend.llm.completions_client import CompletionsClient

ANSWER_PREFIXES = (
    "answer:",
    "response:",
    "our answer",
    "our response",
    "yes,",
    "no,",
    "we ",
    "our ",
    "the firm",
    "the company",
)

BLOCK_CLASSIFIER_PROMPT = """
You analyze questionnaire-style DOCX documents. Each block of text can be a
question, an answer to the most recent question, or unrelated/other.

Consider the block content, the last confirmed question (if any), and the
heuristic suggestion provided. Produce a short JSON object with keys:
  - label: one of ["question", "answer", "none"]
  - reason: brief justification referring to clues in the text

Be decisive—choose the label that best fits the block's role. Always respond
with valid JSON.
""".strip()


@dataclass
class BlockRecord:
    """Lightweight representation of a DOCX block for classification."""

    index: int
    block_type: Literal["paragraph", "table"]
    text: str


@dataclass
class BlockClassification:
    """Classification details for a single block of text."""

    index: int
    block_type: str
    classification: Literal["question", "answer", "none"]
    text: str
    reason: str
    question_reference: Optional[str] = None


@dataclass
class QuestionAnswerBundle:
    """Aggregated mapping between a question and its associated answers."""

    question: str
    question_block: int
    question_reason: str
    answers: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ExtractionResult:
    """Structured response for downstream consumers."""

    questions: List[Dict[str, Any]]
    details: Dict[str, Any]
    classifications: List[BlockClassification]
    qa_bundles: List[QuestionAnswerBundle]


class LLMBlockClassifier:
    """Lightweight wrapper that asks the LLM to label each block."""

    def __init__(self, model: str):
        self._client = CompletionsClient(model=model)

    def classify(
        self,
        *,
        block_index: int,
        text: str,
        previous_question: Optional[str],
        heuristic_label: str,
        heuristic_reason: str,
    ) -> Optional[Tuple[str, str]]:
        prompt = self._build_prompt(
            block_index=block_index,
            text=text,
            previous_question=previous_question,
            heuristic_label=heuristic_label,
            heuristic_reason=heuristic_reason,
        )
        try:
            response = self._client.get_completion(prompt, json_output=True)
        except Exception:
            return None
        content = response[0] if isinstance(response, tuple) else response
        return self._parse_response(content)

    @staticmethod
    def _build_prompt(
        *,
        block_index: int,
        text: str,
        previous_question: Optional[str],
        heuristic_label: str,
        heuristic_reason: str,
    ) -> str:
        prev_question = previous_question or "<none>"
        snippet = text or "<blank>"
        return (
            f"{BLOCK_CLASSIFIER_PROMPT}\n\n"
            f"Block index: {block_index}\n"
            f"Previous question: {prev_question}\n"
            f"Heuristic suggestion: label='{heuristic_label}' reason='{heuristic_reason}'\n"
            f"Block text:\n{snippet}\n"
        )

    @staticmethod
    def _parse_response(content: str) -> Optional[Tuple[str, str]]:
        try:
            data = json.loads(content)
        except Exception:
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except Exception:
                return None
        label = str(data.get("label", "")).strip().lower()
        if label not in {"question", "answer", "none"}:
            return None
        reason = str(data.get("reason") or "").strip() or "LLM classification."
        return label, reason


class DocxQuestionAnalyzer:
    """Core service that loads a DOCX and returns question/answer classifications."""

    def __init__(
        self,
        *,
        treat_docx_as_text: bool = False,
        model: str = "gpt-5-nano",
        use_llm_classifier: bool = False,
        classifier_model: Optional[str] = None,
    ):
        self.treat_docx_as_text = treat_docx_as_text
        self.model = model
        self._block_classifier = (
            LLMBlockClassifier(model=classifier_model or model)
            if use_llm_classifier
            else None
        )
        self._extractor = QuestionExtractor(llm_client=self._build_llm())

    def _build_llm(self) -> Optional[CompletionsClient]:
        if not self.treat_docx_as_text:
            return None
        return CompletionsClient(model=self.model)

    def extract_from_path(self, docx_path: Path) -> ExtractionResult:
        path = self._validate_path(docx_path)
        questions = self._run_question_extraction(path)
        blocks = self._load_blocks(path)
        classifications = self._classify_blocks(blocks, questions)
        qa_bundles = self._bundle_questions_and_answers(classifications)
        return ExtractionResult(
            questions=questions,
            details=self._extractor.last_details,
            classifications=classifications,
            qa_bundles=qa_bundles,
        )

    def _run_question_extraction(self, path: Path) -> List[Dict[str, Any]]:
        with path.open("rb") as stream:
            return self._extractor.extract(
                stream,
                treat_docx_as_text=self.treat_docx_as_text,
            )

    def _load_blocks(self, path: Path) -> List[BlockRecord]:
        doc = Document(str(path))
        raw_blocks = _expand_doc_blocks(doc)
        text_overrides = self._expanded_block_texts(doc, raw_blocks)
        records: List[BlockRecord] = []
        for idx, block in enumerate(raw_blocks):
            block_type: Literal["paragraph", "table"] = (
                "table" if isinstance(block, Table) else "paragraph"
            )
            text = text_overrides[idx]
            records.append(BlockRecord(index=idx, block_type=block_type, text=text))
        return records

    def _expanded_block_texts(
        self,
        doc: Document,
        expanded_blocks: List[Any],
    ) -> List[str]:
        texts: List[str] = []
        for block in _iter_block_items(doc):
            if isinstance(block, Paragraph) and "\n" in (block.text or ""):
                lines = (block.text or "").splitlines()
                if not lines:
                    texts.append("")
                else:
                    texts.extend([line.strip() for line in lines])
            else:
                texts.append(self._raw_block_text(block))
        if len(texts) != len(expanded_blocks):
            texts = [self._raw_block_text(block) for block in expanded_blocks]
        return texts

    def _raw_block_text(self, block: Paragraph | Table) -> str:
        if isinstance(block, Paragraph):
            return (block.text or "").strip()
        return self._extract_table_text(block)

    @staticmethod
    def _extract_table_text(table: Table) -> str:
        lines: List[str] = []
        try:
            for row in table.rows:
                cells = [
                    (cell.text or "").strip()
                    for cell in row.cells
                    if cell.text and cell.text.strip()
                ]
                if cells:
                    lines.append(" | ".join(cells))
        except Exception:
            return ""
        return "\n".join(lines).strip()

    def _classify_blocks(
        self,
        blocks: List[BlockRecord],
        question_payload: List[Dict[str, Any]],
    ) -> List[BlockClassification]:
        slot_map = self._map_slots_to_blocks(blocks, question_payload)
        has_slot_metadata = bool(slot_map)
        classifications: List[BlockClassification] = []
        active_question: Optional[str] = None
        distance_from_question: Optional[int] = None
        trailing_blank_blocks = 0

        for block in blocks:
            if distance_from_question is not None:
                distance_from_question += 1
            text = block.text.strip()
            slot_hits = slot_map.get(block.index, [])
            heuristic_question = self._looks_like_question(text)

            if not text:
                trailing_blank_blocks += 1
            else:
                trailing_blank_blocks = 0
            if trailing_blank_blocks >= 2:
                active_question = None
                distance_from_question = None

            heur_label, heur_reason, question_candidate = self._heuristic_label(
                block=block,
                text=text,
                slot_hits=slot_hits,
                heuristic_question=heuristic_question,
                active_question=active_question,
                offset=distance_from_question,
                has_slot_metadata=has_slot_metadata,
            )

            final_label = heur_label
            final_reason = heur_reason
            if self._block_classifier:
                llm_result = self._block_classifier.classify(
                    block_index=block.index,
                    text=text,
                    previous_question=active_question,
                    heuristic_label=heur_label,
                    heuristic_reason=heur_reason,
                )
                if llm_result:
                    final_label, final_reason = llm_result

            question_reference = self._determine_question_reference(
                label=final_label,
                question_candidate=question_candidate or text or None,
                active_question=active_question,
            )

            classification = BlockClassification(
                index=block.index,
                block_type=block.block_type,
                classification=final_label,
                text=text,
                reason=final_reason,
                question_reference=question_reference,
            )
            classifications.append(classification)

            if final_label == "question":
                active_question = question_reference
                distance_from_question = 0
                trailing_blank_blocks = 0
            elif final_label == "answer":
                # keep active_question for downstream answers
                pass
            else:
                # 'none' keeps current context until blank reset
                pass
        return classifications

    def _heuristic_label(
        self,
        *,
        block: BlockRecord,
        text: str,
        slot_hits: List[Dict[str, Any]],
        heuristic_question: bool,
        active_question: Optional[str],
        offset: Optional[int],
        has_slot_metadata: bool,
    ) -> Tuple[str, str, Optional[str]]:
        if slot_hits:
            question_text = self._slot_question_text(slot_hits, text)
            reason = self._question_reason(slot_hits, block.index)
            return "question", reason, question_text

        if heuristic_question and not has_slot_metadata:
            reason = "Heuristic question phrasing detected in text-extraction mode."
            return "question", reason, text or None

        answer_reason = self._answer_reason(
            block=block,
            text=text,
            active_question=active_question,
            offset=offset,
        )
        if answer_reason:
            return "answer", answer_reason, None

        none_reason = self._none_reason(
            text=text,
            has_question_context=active_question is not None,
            heuristic_question=heuristic_question,
        )
        return "none", none_reason, None

    @staticmethod
    def _determine_question_reference(
        *,
        label: str,
        question_candidate: Optional[str],
        active_question: Optional[str],
    ) -> Optional[str]:
        if label == "question":
            return question_candidate
        if label == "answer":
            return active_question
        return active_question if active_question else None

    @staticmethod
    def _bundle_questions_and_answers(
        classifications: List[BlockClassification],
    ) -> List[QuestionAnswerBundle]:
        bundles: List[QuestionAnswerBundle] = []
        active_bundle: Optional[QuestionAnswerBundle] = None
        for entry in classifications:
            if entry.classification == "question":
                active_bundle = QuestionAnswerBundle(
                    question=entry.text or "",
                    question_block=entry.index,
                    question_reason=entry.reason,
                )
                bundles.append(active_bundle)
                continue
            if entry.classification == "answer" and active_bundle:
                active_bundle.answers.append(
                    {
                        "text": entry.text,
                        "reason": entry.reason,
                        "block": entry.index,
                    }
                )
        return bundles

    def _map_slots_to_blocks(
        self,
        blocks: List[BlockRecord],
        question_payload: List[Dict[str, Any]],
    ) -> Dict[int, List[Dict[str, Any]]]:
        slot_map: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        unmatched: List[Dict[str, Any]] = []

        for slot in question_payload:
            meta = slot.get("meta") or {}
            q_block = meta.get("q_block")
            if q_block is not None:
                slot_map[q_block].append(slot)
            else:
                unmatched.append(slot)

        if not unmatched:
            return slot_map

        normalized_blocks: Dict[str, List[int]] = defaultdict(list)
        for block in blocks:
            normalized = self._normalize_for_match(block.text)
            if normalized:
                normalized_blocks[normalized].append(block.index)

        for slot in unmatched:
            normalized_question = self._normalize_for_match(
                slot.get("question") or slot.get("question_text") or ""
            )
            if not normalized_question:
                continue
            candidates = normalized_blocks.get(normalized_question)
            if not candidates:
                continue
            block_index = candidates.pop(0)
            if not candidates:
                normalized_blocks.pop(normalized_question, None)
            slot_map[block_index].append(slot)

        return slot_map

    @staticmethod
    def _slot_question_text(slots: List[Dict[str, Any]], fallback: str) -> str:
        for slot in slots:
            text = (
                slot.get("question")
                or slot.get("question_text")
                or slot.get("question_label")
            )
            if text:
                return text.strip()
        return fallback

    @staticmethod
    def _question_reason(slots: List[Dict[str, Any]], block_index: int) -> str:
        detectors = sorted(
            {
                (slot.get("meta") or {}).get("detector", "unknown")
                for slot in slots
                if slot.get("meta")
            }
        )
        detector_fragment = f"detectors: {', '.join(detectors)}" if detectors else "slot metadata"
        return f"Identified as question at block {block_index} via {detector_fragment}."

    def _answer_reason(
        self,
        *,
        block: BlockRecord,
        text: str,
        active_question: Optional[str],
        offset: Optional[int],
    ) -> Optional[str]:
        if (
            active_question is None
            or not text
            or offset is None
            or offset > 8
            or self._looks_like_question(text)
        ):
            return None

        signals: List[str] = []
        if self._looks_like_answer(text):
            signals.append("answer-like phrasing detected")
        if block.block_type == "table":
            signals.append("table block contains populated cells")
        if len(text.split()) >= 6:
            signals.append("multi-word content immediately after the question")

        if not signals:
            return None

        signal_str = "; ".join(signals)
        return (
            f"Linked to previous question '{active_question}' "
            f"(offset {offset}) because {signal_str}."
        )

    @staticmethod
    def _none_reason(
        *,
        text: str,
        has_question_context: bool,
        heuristic_question: bool,
    ) -> str:
        fragments = ["No question slot match"]
        if has_question_context:
            fragments.append("inside question context but lacked answer signals")
        else:
            fragments.append("outside any active question context")
        if not text:
            fragments.append("block is empty")
        elif heuristic_question:
            fragments.append("question-like phrasing already accounted for elsewhere")
        return "; ".join(fragments) + "."

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        return " ".join(DocxQuestionAnalyzer._strip_leading_number(text).split()).lower()

    @staticmethod
    def _strip_leading_number(text: str) -> str:
        return re.sub(r"^\s*\d+[\.\)]\s*", "", text or "")

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        clean = (text or "").strip()
        if not clean:
            return False
        lower = clean.lower()
        if clean.endswith("?"):
            return True
        if lower.startswith(("who", "what", "where", "when", "why", "how")):
            return True
        return any(phrase in lower for phrase in QUESTION_PHRASES)

    @staticmethod
    def _looks_like_answer(text: str) -> bool:
        clean = (text or "").strip().lower()
        if not clean:
            return False
        if clean.startswith(ANSWER_PREFIXES):
            return True
        if " we " in f" {clean} " or " our " in f" {clean} ":
            return True
        return len(clean.split()) >= 10

    @staticmethod
    def _validate_path(docx_path: Path) -> Path:
        path = Path(docx_path)
        if not path.exists():
            raise SystemExit(f"DOCX file not found: {path}")
        if not path.is_file():
            raise SystemExit(f"Expected a file path, got: {path}")
        return path


class DocxQuestionCLI:
    """Thin wrapper that renders ExtractionResult instances."""

    def __init__(
        self,
        *,
        show_metadata: bool = False,
        output_json: bool = False,
    ):
        self.show_metadata = show_metadata
        self.output_json = output_json

    def render(self, result: ExtractionResult) -> None:
        if self.output_json:
            print(
                json.dumps(
                    {
                        "questions": result.questions,
                        "details": result.details,
                        "classifications": [asdict(cls) for cls in result.classifications],
                        "qa_bundles": [
                            {
                                "question": bundle.question,
                                "question_block": bundle.question_block,
                                "question_reason": bundle.question_reason,
                                "answers": bundle.answers,
                            }
                            for bundle in result.qa_bundles
                        ],
                    },
                    indent=2,
                )
            )
            return
        self._print_human_readable(result)

    def _print_human_readable(self, result: ExtractionResult) -> None:
        if not result.questions:
            print("No questions were detected in the document.")
        else:
            print("Detected questions:")
            for idx, entry in enumerate(result.questions, start=1):
                print(f"{idx}. {self._format_question(entry)}")
                if self.show_metadata:
                    metadata = {
                        key: value
                        for key, value in entry.items()
                        if key != "question"
                    }
                    if metadata:
                        print(json.dumps(metadata, indent=2))

        print("\nBlock classifications:")
        if not result.classifications:
            print("  (no blocks parsed)")
            return
        for entry in result.classifications:
            label = entry.classification.upper()
            text = entry.text or "<blank>"
            question_ref = (
                f" (question: {entry.question_reference})"
                if entry.question_reference and entry.classification != "question"
                else ""
            )
            print(
                f"[{label:<7}] Block {entry.index} ({entry.block_type}){question_ref}: {text}"
            )
            print(f"          Reason: {entry.reason}")

        print("\nQuestion → Answer map:")
        if not result.qa_bundles:
            print("  (no questions identified)")
        else:
            for bundle in result.qa_bundles:
                print(
                    f"? Block {bundle.question_block}: {bundle.question} "
                    f"(reason: {bundle.question_reason})"
                )
                if not bundle.answers:
                    print("    ↳ No answers captured for this question.")
                    continue
                for answer in bundle.answers:
                    print(
                        f"    ↳ Answer block {answer['block']}: {answer['text']} "
                        f"(reason: {answer['reason']})"
                    )

    @staticmethod
    def _format_question(entry: Dict[str, Any]) -> str:
        question = (entry.get("question") or "").strip()
        return question or "<empty question text>"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract questions from a DOCX using the shared QuestionExtractor logic.",
    )
    parser.add_argument(
        "docx_path",
        type=Path,
        help="Path to the DOCX file to inspect.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw question payload and classifications as JSON.",
    )
    parser.add_argument(
        "--show-metadata",
        action="store_true",
        help="Print slot metadata under each question for quick debugging.",
    )
    parser.add_argument(
        "--treat-docx-as-text",
        action="store_true",
        help=(
            "Skip the DOCX slot heuristics and instead run the text-based extractor. "
            "Requires the custom LLM client credentials."
        ),
    )
    parser.add_argument(
        "--model",
        default="gpt-5-nano",
        help="LLM model name for --treat-docx-as-text (ignored otherwise).",
    )
    parser.add_argument(
        "--use-llm-classifier",
        action="store_true",
        help="Use the LLM to classify each block as question/answer/none.",
    )
    parser.add_argument(
        "--classifier-model",
        default=None,
        help="Optional override model ID for the block classifier.",
    )
    return parser.parse_args()


def _consume_args_or_defaults(
    docx_path: Optional[Path],
    *,
    treat_docx_as_text: Optional[bool],
    model: Optional[str],
    output_json: Optional[bool],
    show_metadata: Optional[bool],
    use_llm_classifier: Optional[bool],
    classifier_model: Optional[str],
) -> Dict[str, Any]:
    if docx_path is not None:
        return {
            "docx_path": Path(docx_path),
            "treat_docx_as_text": bool(treat_docx_as_text),
            "model": model or "gpt-5-nano",
            "output_json": bool(output_json),
            "show_metadata": bool(show_metadata),
            "use_llm_classifier": bool(use_llm_classifier),
            "classifier_model": classifier_model or model or "gpt-5-nano",
            "cli_mode": False,
        }
    args = _parse_args()
    return {
        "docx_path": args.docx_path,
        "treat_docx_as_text": args.treat_docx_as_text,
        "model": args.model,
        "output_json": args.json,
        "show_metadata": args.show_metadata,
        "use_llm_classifier": args.use_llm_classifier,
        "classifier_model": args.classifier_model or args.model,
        "cli_mode": True,
    }


def main(
    docx_path: Optional[Path] = None,
    *,
    treat_docx_as_text: Optional[bool] = None,
    model: Optional[str] = None,
    output_json: Optional[bool] = None,
    show_metadata: Optional[bool] = None,
    use_llm_classifier: Optional[bool] = None,
    classifier_model: Optional[str] = None,
) -> ExtractionResult:
    """
    Primary entry point so other modules can call `main(Path("foo.docx"))`
    and receive the extracted questions along with block classifications.
    """
    cfg = _consume_args_or_defaults(
        docx_path,
        treat_docx_as_text=treat_docx_as_text,
        model=model,
        output_json=output_json,
        show_metadata=show_metadata,
        use_llm_classifier=use_llm_classifier,
        classifier_model=classifier_model,
    )

    analyzer = DocxQuestionAnalyzer(
        treat_docx_as_text=cfg["treat_docx_as_text"],
        model=cfg["model"],
        use_llm_classifier=cfg["use_llm_classifier"],
        classifier_model=cfg["classifier_model"],
    )
    result = analyzer.extract_from_path(cfg["docx_path"])

    if cfg["cli_mode"]:
        renderer = DocxQuestionCLI(
            show_metadata=cfg["show_metadata"],
            output_json=cfg["output_json"],
        )
        renderer.render(result)

    return result


if __name__ == "__main__":
    main()
