"""
docx_question_reader.py

Object-oriented helper that mirrors the backend QuestionExtractor logic so DOCX
files can be inspected programmatically or via a simple CLI entry point.

Examples:
    python backend/documents/extraction/docx_question_reader.py path/to/file.docx
    python backend/documents/extraction/docx_question_reader.py file.docx --json
    python backend/documents/extraction/docx_question_reader.py file.docx --show-metadata

Use the --treat-docx-as-text flag to force the fallback text-extraction path.
That mode requires the custom CompletionsClient environment variables because
it calls the LLM prompt used in `QuestionExtractor.extract_from_text`.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.documents.extraction.question_extractor import QuestionExtractor
from backend.llm.completions_client import CompletionsClient


@dataclass
class ExtractionResult:
    """Structured response for downstream consumers."""

    questions: List[Dict[str, Any]]
    details: Dict[str, Any]


class DocxQuestionAnalyzer:
    """Core service that loads a DOCX and returns its detected questions."""

    def __init__(self, *, treat_docx_as_text: bool = False, model: str = "gpt-5-nano"):
        self.treat_docx_as_text = treat_docx_as_text
        self.model = model
        self._extractor = QuestionExtractor(llm_client=self._build_llm())

    def _build_llm(self) -> Optional[CompletionsClient]:
        if not self.treat_docx_as_text:
            return None
        return CompletionsClient(model=self.model)

    def extract_from_path(self, docx_path: Path) -> ExtractionResult:
        path = self._validate_path(docx_path)
        with path.open("rb") as stream:
            payload = self._extractor.extract(
                stream,
                treat_docx_as_text=self.treat_docx_as_text,
            )
        return ExtractionResult(
            questions=payload,
            details=self._extractor.last_details,
        )

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
                    {"questions": result.questions, "details": result.details},
                    indent=2,
                )
            )
            return
        self._print_human_readable(result.questions)

    def _print_human_readable(self, questions: List[Dict[str, Any]]) -> None:
        if not questions:
            print("No questions were detected in the document.")
            return
        for idx, entry in enumerate(questions, start=1):
            print(f"{idx}. {self._format_question(entry)}")
            if self.show_metadata:
                metadata = {
                    key: value
                    for key, value in entry.items()
                    if key != "question"
                }
                if metadata:
                    print(json.dumps(metadata, indent=2))

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
        help="Print the raw question payload as JSON (includes metadata).",
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
    return parser.parse_args()


def _consume_args_or_defaults(
    docx_path: Optional[Path],
    *,
    treat_docx_as_text: Optional[bool],
    model: Optional[str],
    output_json: Optional[bool],
    show_metadata: Optional[bool],
) -> Dict[str, Any]:
    if docx_path is not None:
        return {
            "docx_path": Path(docx_path),
            "treat_docx_as_text": bool(treat_docx_as_text),
            "model": model or "gpt-5-nano",
            "output_json": bool(output_json),
            "show_metadata": bool(show_metadata),
            "cli_mode": False,
        }
    args = _parse_args()
    return {
        "docx_path": args.docx_path,
        "treat_docx_as_text": args.treat_docx_as_text,
        "model": args.model,
        "output_json": args.json,
        "show_metadata": args.show_metadata,
        "cli_mode": True,
    }


def main(
    docx_path: Optional[Path] = None,
    *,
    treat_docx_as_text: Optional[bool] = None,
    model: Optional[str] = None,
    output_json: Optional[bool] = None,
    show_metadata: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """
    Primary entry point so other modules can call `main(Path("foo.docx"))`
    and receive the extracted questions.
    """
    cfg = _consume_args_or_defaults(
        docx_path,
        treat_docx_as_text=treat_docx_as_text,
        model=model,
        output_json=output_json,
        show_metadata=show_metadata,
    )

    analyzer = DocxQuestionAnalyzer(
        treat_docx_as_text=cfg["treat_docx_as_text"],
        model=cfg["model"],
    )
    result = analyzer.extract_from_path(cfg["docx_path"])

    if cfg["cli_mode"]:
        renderer = DocxQuestionCLI(
            show_metadata=cfg["show_metadata"],
            output_json=cfg["output_json"],
        )
        renderer.render(result)

    return result.questions


if __name__ == "__main__":
    main()
