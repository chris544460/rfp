#!/usr/bin/env python3
"""CLI analogue of the Streamlit RFP Responder app.

This script mirrors the behaviour of the Streamlit notebook UI while staying
entirely terminal-driven. It supports two primary entry points:

- ``ask``: interactive or one-shot question answering, optionally retrieving
  the closest pre-approved answers instead of generating new content.
- ``document``: process an uploaded RFP document (Excel, Word, PDF, or text)
  and emit answered files similar to the Streamlit workflow.

The implementation reuses the same backend helpers as the Streamlit notebook
so results stay consistent across interfaces.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from textwrap import dedent
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from cli_app import build_docx, extract_questions, load_input_text
from qa_core import answer_question, collect_relevant_snippets
from answer_composer import CompletionsClient, get_openai_completion
from input_file_reader.interpreter_sheet import collect_non_empty_cells
from rfp_xlsx_slot_finder import ask_sheet_schema
from rfp_xlsx_apply_answers import write_excel_answers
from rfp_docx_slot_finder import extract_slots_from_docx
from rfp_docx_apply_answers import apply_answers_to_docx


# ---------------------------------------------------------------------------
# Model presets & option metadata
# ---------------------------------------------------------------------------

MODEL_DESCRIPTIONS: Dict[str, str] = {
    "gpt-4.1-nano-2025-04-14_research": "Lighter, faster model",
    "o3-2025-04-16_research": "Slower, reasoning model",
}

MODEL_SHORT_NAMES: Dict[str, str] = {
    "gpt-4.1-nano-2025-04-14_research": "4.1",
    "o3-2025-04-16_research": "o3",
}

MODEL_OPTIONS: Sequence[str] = tuple(MODEL_DESCRIPTIONS.keys())
DEFAULT_MODEL = "o3-2025-04-16_research"
DEFAULT_INDEX = 0 if DEFAULT_MODEL not in MODEL_OPTIONS else MODEL_OPTIONS.index(DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------


class OpenAIClient:
    """Proxy that mirrors the CompletionsClient interface for OpenAI models."""

    def __init__(self, model: str):
        self.model = model

    def get_completion(self, prompt: str, json_output: bool = False):
        return get_openai_completion(prompt, self.model, json_output=json_output)


def resolve_llm_client(framework: str, model: str):
    framework = framework.lower()
    if framework not in {"aladdin", "openai"}:
        raise ValueError(f"Unsupported framework '{framework}'. Choose 'aladdin' or 'openai'.")
    if framework == "aladdin":
        return CompletionsClient(model=model)
    return OpenAIClient(model=model)


def load_fund_tags() -> List[str]:
    path = Path("~/derivs-tool/rfp-ai-tool/structured_extraction/embedding_data.json").expanduser()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    tags = {t for item in data for t in item.get("metadata", {}).get("tags", [])}
    return sorted(tags)


# ---------------------------------------------------------------------------
# Answer generation helpers
# ---------------------------------------------------------------------------


def build_generator(
    search_mode: str,
    fund: Optional[str],
    k: int,
    length: str,
    approx_words: Optional[int],
    min_confidence: float,
    include_citations: bool,
    llm,
    extra_docs: Optional[List[str]] = None,
):
    def gen(question: str, progress: Optional[Callable[[str], None]] = None):
        answer, citations = answer_question(
            question,
            search_mode,
            fund,
            k,
            length,
            approx_words,
            min_confidence,
            llm,
            extra_docs=extra_docs,
            progress=progress,
        )
        if not include_citations:
            answer = re.sub(r"\[\d+\]", "", answer)
            return answer
        citations_map = {
            label: {"text": snippet, "source_file": source}
            for label, source, snippet, score, date_str in citations
        }
        return {"text": answer, "citations": citations_map}

    return gen


def select_top_preapproved_answers(question: str, hits: List[Dict[str, object]], limit: int = 5) -> List[Dict[str, object]]:
    """Mirror the Streamlit reranker flow for pre-approved hits."""
    if len(hits) <= limit:
        return hits

    formatted = []
    for idx, hit in enumerate(hits, start=1):
        snippet = (hit.get("snippet") or "").strip()
        if len(snippet) > 500:
            snippet = snippet[:497] + "..."
        source = hit.get("source") or "unknown"
        score = hit.get("score")
        score_repr = f"{score:.3f}" if isinstance(score, (int, float)) else str(score or "unknown")
        date = hit.get("date") or "unknown"
        formatted.append(
            dedent(
                f"""
                {idx}. Source: {source}
                   Score: {score_repr}
                   Date: {date}
                   Snippet: {snippet}
                """
            ).strip()
        )

    prompt = (
        "You are ranking pre-approved RFP answers for how well they address a user's question. "
        f"Return a JSON object with a 'selections' array containing up to {limit} items. "
        "Each selection must include an 'index' (1-based) pointing to the candidate and a 'reason' in one or two sentences "
        "explaining how the candidate addresses the user's question."
        f"\n\nQuestion: {question}"
        "\n\nCandidates:\n" + "\n\n".join(formatted)
    )

    model_name = os.environ.get("ALADDIN_RERANK_MODEL", "o3-2025-04-16_research")
    try:
        client = CompletionsClient(model=model_name)
        content, _ = client.get_completion(prompt, json_output=True)
        data = json.loads(content or "{}")
    except Exception as exc:
        print(f"[WARN] Failed to rerank candidates with {model_name}: {exc}")
        return hits[:limit]

    selected: List[Dict[str, object]] = []
    seen = set()

    def add_hit(position: int, reason: Optional[str] = None) -> None:
        if not isinstance(position, int) or not (1 <= position <= len(hits)) or position in seen:
            return
        seen.add(position)
        payload = dict(hits[position - 1])
        if reason:
            cleaned = " ".join(str(reason).split())
            if cleaned:
                payload["selection_reason"] = cleaned
        payload.setdefault("selected_by_model", model_name)
        selected.append(payload)

    selections = (
        data.get("selections")
        or data.get("choices")
        or data.get("ranked")
        or data.get("results")
        or []
    )
    if isinstance(selections, dict):
        for value in selections.values():
            if isinstance(value, list):
                selections = value
                break

    if isinstance(selections, list):
        for entry in selections:
            if len(selected) == limit:
                break
            if isinstance(entry, dict):
                reason = entry.get("reason") or entry.get("rationale") or entry.get("why")
                idx_value = (
                    entry.get("index")
                    or entry.get("idx")
                    or entry.get("rank")
                    or entry.get("position")
                )
            else:
                reason = None
                idx_value = entry
            try:
                pos = int(idx_value)
            except (TypeError, ValueError):
                continue
            add_hit(pos, reason)

    if len(selected) < limit:
        indices = data.get("top_indices") or data.get("top") or data.get("indices") or []
        if isinstance(indices, Iterable):
            for idx in indices:
                if len(selected) == limit:
                    break
                try:
                    add_hit(int(idx))
                except (TypeError, ValueError):
                    continue

    if len(selected) < limit:
        for position in range(1, len(hits) + 1):
            if len(selected) == limit:
                break
            if position in seen:
                continue
            add_hit(position)

    return selected[:limit] if selected else hits[:limit]


# ---------------------------------------------------------------------------
# Document processing flows
# ---------------------------------------------------------------------------


def ensure_paths_exist(paths: Optional[Sequence[str]]) -> Optional[List[str]]:
    if not paths:
        return None
    resolved: List[str] = []
    for item in paths:
        p = Path(item).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Extra document not found: {p}")
        resolved.append(str(p))
    return resolved


def process_excel(
    input_path: Path,
    output_dir: Path,
    generator,
    include_citations: bool,
    show_live: bool,
) -> Dict[str, object]:
    print("[INFO] Reading Excel workbook …")
    collect_non_empty_cells(str(input_path))
    schema = ask_sheet_schema(str(input_path))
    answers = []
    total_questions = len(schema)
    for idx, entry in enumerate(schema, start=1):
        question = (entry.get("question_text") or "").strip()
        if show_live and question:
            print(f"[Q{idx}] {question}")
        answer = generator(question)
        answers.append(answer)
        if show_live:
            text = answer.get("text", "") if isinstance(answer, dict) else answer
            print(f"  ↳ {text}\n")
        else:
            print(f"[INFO] Answered {idx}/{total_questions}", end="\r", flush=True)
    print()

    output_dir.mkdir(parents=True, exist_ok=True)
    answered_path = output_dir / f"{input_path.stem}_answered.xlsx"
    write_excel_answers(
        schema,
        answers,
        str(input_path),
        str(answered_path),
        include_comments=include_citations,
    )
    comments_path = answered_path.with_name(answered_path.stem + "_comments.docx")

    qa_pairs = []
    for entry, answer in zip(schema, answers):
        question_text = (entry.get("question_text") or "").strip()
        answer_text = answer.get("text", "") if isinstance(answer, dict) else str(answer)
        qa_pairs.append({"question": question_text, "answer": answer_text})

    payload = {
        "mode": "excel",
        "output_file": str(answered_path),
        "comments_file": str(comments_path) if include_citations and comments_path.exists() else None,
        "qa_pairs": qa_pairs,
    }
    return payload


def process_docx_slots(
    input_path: Path,
    output_dir: Path,
    generator,
    include_citations: bool,
    docx_write_mode: str,
    show_live: bool,
) -> Dict[str, object]:
    print("[INFO] Extracting slots from DOCX …")
    slots_payload = extract_slots_from_docx(str(input_path))
    slots = slots_payload.get("slots", [])
    answers_by_id: Dict[str, object] = {}
    total_slots = len(slots)
    for idx, slot in enumerate(slots, start=1):
        question = (slot.get("question_text") or "").strip()
        if show_live and question:
            print(f"[Slot {idx}] {question}")
        answer = generator(question)
        answers_by_id[slot.get("id", f"slot_{idx}")] = answer
        if show_live:
            text = answer.get("text", "") if isinstance(answer, dict) else answer
            print(f"  ↳ {text}\n")
        else:
            print(f"[INFO] Answered {idx}/{total_slots}", end="\r", flush=True)
    print()

    output_dir.mkdir(parents=True, exist_ok=True)
    answered_path = output_dir / f"{input_path.stem}_answered.docx"
    slots_tmp = output_dir / f"{input_path.stem}_slots.json"
    answers_tmp = output_dir / f"{input_path.stem}_answers.json"

    slots_tmp.write_text(json.dumps(slots_payload), encoding="utf-8")
    answers_tmp.write_text(json.dumps({"by_id": answers_by_id}), encoding="utf-8")

    apply_answers_to_docx(
        docx_path=str(input_path),
        slots_json_path=str(slots_tmp),
        answers_json_path=str(answers_tmp),
        out_path=str(answered_path),
        mode=docx_write_mode,
        generator=None,
        gen_name="cli_streamlit_app:rag_gen",
    )

    qa_pairs = []
    for slot in slots:
        question_text = (slot.get("question_text") or "").strip()
        answer_obj = answers_by_id.get(slot.get("id"))
        if isinstance(answer_obj, dict):
            answer_text = answer_obj.get("text", "")
        else:
            answer_text = str(answer_obj or "")
        qa_pairs.append({"question": question_text, "answer": answer_text})

    payload = {
        "mode": "docx_slots",
        "output_file": str(answered_path),
        "slots_dump": str(slots_tmp),
        "answers_dump": str(answers_tmp),
        "qa_pairs": qa_pairs,
        "include_citations": include_citations,
        "docx_write_mode": docx_write_mode,
    }
    return payload


def process_textish(
    input_path: Path,
    output_dir: Path,
    llm,
    search_mode: str,
    fund: Optional[str],
    k: int,
    length: str,
    approx_words: Optional[int],
    min_confidence: float,
    include_citations: bool,
    extra_docs: Optional[List[str]],
    show_live: bool,
) -> Dict[str, object]:
    print("[INFO] Loading document text …")
    raw_text = load_input_text(str(input_path))
    questions = extract_questions(raw_text, llm)

    if not questions:
        print("[WARN] No questions could be extracted from the document.")
        return {
            "mode": "document_summary",
            "output_file": None,
            "qa_pairs": [],
        }

    answers: List[object] = []
    comments: List[List[Tuple[str, str, str, float, str]]] = []
    total = len(questions)
    for idx, question in enumerate(questions, start=1):
        if show_live:
            print(f"[Q{idx}] {question}")
        answer, cmts = answer_question(
            question,
            search_mode,
            fund,
            k,
            length,
            approx_words,
            min_confidence,
            llm,
            extra_docs=extra_docs,
        )
        if not include_citations:
            answer = re.sub(r"\[\d+\]", "", answer)
            cmts = []
        answers.append(answer)
        comments.append(cmts)
        if show_live:
            print(f"  ↳ {answer}\n")
        else:
            print(f"[INFO] Answered {idx}/{total}", end="\r", flush=True)
    if not show_live:
        print()

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{input_path.stem}_answered.docx"
    report_bytes = build_docx(questions, answers, comments, include_comments=include_citations)
    output_path.write_bytes(report_bytes)

    qa_pairs = []
    for question, answer in zip(questions, answers):
        if isinstance(answer, dict):
            text = answer.get("text", "")
        else:
            text = str(answer)
        qa_pairs.append({"question": question, "answer": text})

    return {
        "mode": "document_summary",
        "output_file": str(output_path),
        "qa_pairs": qa_pairs,
    }


# ---------------------------------------------------------------------------
# CLI command implementations
# ---------------------------------------------------------------------------


def run_question_mode(args: argparse.Namespace) -> None:
    framework = args.framework or os.getenv("ANSWER_FRAMEWORK", "aladdin")
    model = args.model or (MODEL_OPTIONS[DEFAULT_INDEX] if MODEL_OPTIONS else DEFAULT_MODEL)
    llm = resolve_llm_client(framework, model)
    extra_docs = ensure_paths_exist(args.extra_doc)
    include_citations = args.include_citations
    generator = build_generator(
        search_mode=args.search_mode,
        fund=args.fund or None,
        k=args.k,
        length=args.length,
        approx_words=args.approx_words,
        min_confidence=args.min_confidence,
        include_citations=include_citations,
        llm=llm,
        extra_docs=extra_docs,
    )

    def answer_once(question: str) -> None:
        if args.response_mode == "preapproved":
            rows = collect_relevant_snippets(
                q=question,
                mode=args.search_mode,
                fund=args.fund or None,
                k=args.k,
                min_confidence=args.min_confidence,
                llm=llm,
                extra_docs=extra_docs,
                progress=lambda msg: print(f"[PROGRESS] {msg}", end="\r", flush=True),
            )
            print()
            hits = []
            for label, source, snippet, score, date_str in rows:
                hits.append(
                    {
                        "label": label,
                        "source": source,
                        "snippet": snippet,
                        "score": score,
                        "date": date_str,
                    }
                )
            hits = select_top_preapproved_answers(question, hits)
            if not hits:
                print("[INFO] No relevant pre-approved answers found.")
                return
            for idx, hit in enumerate(hits, start=1):
                print(f"--- Result {idx} ---")
                print(f"Source: {hit.get('source', 'Unknown')}")
                if hit.get("selected_by_model"):
                    print(f"Selected by: {hit['selected_by_model']}")
                reason = hit.get("selection_reason")
                if reason:
                    print(f"Reason: {reason}")
                snippet = hit.get("snippet")
                if snippet:
                    print(snippet)
                score = hit.get("score")
                if score is not None:
                    print(f"Score: {score}")
                date_str = hit.get("date")
                if date_str:
                    print(f"Date: {date_str}")
                print()
            return

        payload = generator(question)
        if isinstance(payload, dict):
            print(payload.get("text", ""))
            citations = payload.get("citations") or {}
            if citations:
                print("\nCitations:")
                for label, cite in citations.items():
                    meta = []
                    src = cite.get("source_file")
                    if src:
                        meta.append(f"Source: {src}")
                    snippet = cite.get("text")
                    if snippet:
                        meta.append(f"Snippet: {snippet}")
                    print(f"  [{label}] " + " | ".join(meta))
        else:
            print(payload)

    if args.question:
        answer_once(args.question)
        return

    print("[INFO] Enter questions (Ctrl-D to exit).")
    try:
        while True:
            prompt = input("Question> ").strip()
            if not prompt:
                continue
            answer_once(prompt)
            print()
    except (EOFError, KeyboardInterrupt):
        print("\n[INFO] Exiting question mode.")


def run_document_mode(args: argparse.Namespace) -> None:
    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    framework = args.framework or os.getenv("ANSWER_FRAMEWORK", "aladdin")
    model = args.model or (MODEL_OPTIONS[DEFAULT_INDEX] if MODEL_OPTIONS else DEFAULT_MODEL)
    llm = resolve_llm_client(framework, model)
    extra_docs = ensure_paths_exist(args.extra_doc)
    include_citations = args.include_citations
    generator = build_generator(
        search_mode=args.search_mode,
        fund=args.fund or None,
        k=args.k,
        length=args.length,
        approx_words=args.approx_words,
        min_confidence=args.min_confidence,
        include_citations=include_citations,
        llm=llm,
        extra_docs=extra_docs,
    )

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else input_path.parent

    suffix = input_path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        result = process_excel(input_path, output_dir, generator, include_citations, args.show_live)
    elif suffix == ".docx" and not args.docx_as_text:
        result = process_docx_slots(
            input_path,
            output_dir,
            generator,
            include_citations,
            args.docx_write_mode,
            args.show_live,
        )
    else:
        result = process_textish(
            input_path,
            output_dir,
            llm,
            args.search_mode,
            args.fund or None,
            args.k,
            args.length,
            args.approx_words,
            args.min_confidence,
            include_citations,
            extra_docs,
            args.show_live,
        )

    print("\n[INFO] Document processing complete.")
    for key, value in result.items():
        if value is None:
            continue
        if key.endswith("file") or key.endswith("_dump"):
            print(f"  {key}: {value}")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fund", help="Fund or strategy tag to scope answers")
    common.add_argument("--framework", choices=["aladdin", "openai"], help="Backend framework to use")
    common.add_argument("--model", choices=list(MODEL_OPTIONS), help="Model to use for generation")
    common.add_argument("--search-mode", default="both", help="Search mode passed to backend")
    common.add_argument("--k", type=int, default=20, help="Maximum retrieved hits per question")
    common.add_argument("--min-confidence", type=float, default=0.0, help="Minimum document score")
    common.add_argument("--length", choices=["auto", "short", "medium", "long"], default="long")
    common.add_argument("--approx-words", dest="approx_words", type=int, help="Approximate answer length in words")
    common.add_argument(
        "--include-citations",
        dest="include_citations",
        action="store_true",
        default=True,
        help="Include citation metadata in responses",
    )
    common.add_argument(
        "--no-include-citations",
        dest="include_citations",
        action="store_false",
        help="Omit citation metadata from responses",
    )
    common.add_argument(
        "--extra-doc",
        dest="extra_doc",
        action="append",
        help="Additional documents to treat as supplemental context",
    )

    parser = argparse.ArgumentParser(
        description="CLI equivalent of the Streamlit RFP Responder app",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ask = subparsers.add_parser(
        "ask",
        parents=[common],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        help="Chat-style question answering",
    )
    ask.add_argument("--question", help="Single question to answer; omit for interactive mode")
    ask.add_argument(
        "--response-mode",
        choices=["generate", "preapproved"],
        default="generate",
        help="Choose between generating answers or retrieving closest approved snippets",
    )

    doc = subparsers.add_parser(
        "document",
        parents=[common],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        help="Process a document upload flow",
    )
    doc.add_argument("input", help="Path to the document to process")
    doc.add_argument("--output-dir", help="Directory for generated files (defaults to the input directory)")
    doc.add_argument("--docx-as-text", action="store_true", help="Treat DOCX files as plain text")
    doc.add_argument(
        "--docx-write-mode",
        choices=["fill", "replace", "append"],
        default="fill",
        help="How answers are written back to DOCX templates",
    )
    doc.add_argument("--show-live", action="store_true", help="Print each question/answer as it is produced")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ask":
        run_question_mode(args)
    elif args.command == "document":
        run_document_mode(args)
    else:
        parser.error("Unknown command")


if __name__ == "__main__":
    main()
