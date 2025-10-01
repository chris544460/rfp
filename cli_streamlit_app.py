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
import docx
from docx.text.paragraph import Paragraph

from cli_app import build_docx, extract_questions, load_input_text
from qa_core import answer_question, collect_relevant_snippets
from answer_composer import CompletionsClient, get_openai_completion
from input_file_reader.interpreter_sheet import collect_non_empty_cells
from rfp_xlsx_slot_finder import ask_sheet_schema
from rfp_xlsx_apply_answers import write_excel_answers
from rfp_docx_slot_finder import (
    QUESTION_PHRASES,
    USE_SPACY_QUESTION,
    extract_slots_from_docx,
    strip_enum_prefix,
    _ENUM_PREFIX_RE,
    _iter_block_items,
    _spacy_docx_is_question,
)
from rfp_docx_apply_answers import apply_answers_to_docx


DEBUG_ENABLED = os.getenv("CLI_STREAMLIT_DEBUG", "0") not in {"", "0", "false", "False"}
ENV_DEBUG_DEFAULT = DEBUG_ENABLED


def set_debug(enabled: bool) -> None:
    global DEBUG_ENABLED
    DEBUG_ENABLED = enabled


def log_debug(message: str) -> None:
    if DEBUG_ENABLED:
        print(f"[DEBUG] {message}")


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
    skipped_slots: List[str] = []
    for idx, slot in enumerate(slots, start=1):
        question = (slot.get("question_text") or "").strip()
        slot_id = slot.get("id", f"slot_{idx}")
        if not question:
            log_debug(f"DOCX slot {slot_id} (index {idx}) had empty question text; storing blank answer")
            if not show_live:
                print(f"[WARN] Slot {idx} has no extracted question; skipping.")
            if show_live:
                print(f"[Slot {idx}] (no question detected; skipped)")
            answers_by_id[slot_id] = ""
            skipped_slots.append(slot_id)
            continue
        if show_live:
            print(f"[Slot {idx}] {question}")
        answer = generator(question)
        answers_by_id[slot_id] = answer
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
    if skipped_slots:
        payload["skipped_slots"] = skipped_slots
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

    processed_questions: List[str] = []
    answers: List[object] = []
    comments: List[List[Tuple[str, str, str, float, str]]] = []
    total = len(questions)
    for idx, question in enumerate(questions, start=1):
        question = question.strip()
        if not question:
            log_debug(f"Text mode question index {idx} was empty; skipping")
            continue
        processed_questions.append(question)
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
    report_bytes = build_docx(processed_questions, answers, comments, include_comments=include_citations)
    output_path.write_bytes(report_bytes)

    qa_pairs = []
    for question, answer in zip(processed_questions, answers):
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
# Question listing helper
# ---------------------------------------------------------------------------


def _diagnose_paragraph(text: str) -> Dict[str, object]:
    raw = (text or "").strip()
    positives: List[str] = []
    negatives: List[str] = []

    if not raw:
        negatives.append("blank paragraph")
        return {
            "looks_like": False,
            "positives": positives,
            "negatives": negatives,
            "ends_with_q": False,
        }

    ends_with_q = raw.rstrip().endswith("?")
    if "?" in raw:
        positives.append("contains '?'")

    stripped = strip_enum_prefix(raw).strip()
    lowered = stripped.lower()

    starts_with = next((phrase for phrase in QUESTION_PHRASES if lowered.startswith(phrase)), None)
    if starts_with:
        positives.append(f"starts with '{starts_with}'")

    contains_cue = next((phrase for phrase in QUESTION_PHRASES if phrase in lowered), None)
    if contains_cue and contains_cue != starts_with:
        positives.append(f"contains '{contains_cue}'")

    if raw.lower().startswith(("question:", "prompt:", "rfp question:")):
        positives.append("prefixed with question label")

    if _ENUM_PREFIX_RE.match(raw) and contains_cue:
        positives.append("enumerated with cue phrase")

    if USE_SPACY_QUESTION and _spacy_docx_is_question(raw):
        positives.append("spaCy detector match")

    looks_like = bool(positives)

    if not looks_like:
        word_count = len(raw.split())
        if word_count < 4:
            negatives.append("fewer than 4 words")
        if "?" not in raw:
            negatives.append("no question mark")
        if not contains_cue:
            negatives.append("no cue phrase detected")
        if raw.endswith(":"):
            negatives.append("ends with ':' (likely label)")
        if raw.isupper() and len(raw) > 6:
            negatives.append("all caps (likely heading)")

    return {
        "looks_like": looks_like,
        "positives": positives,
        "negatives": negatives,
        "ends_with_q": ends_with_q,
    }


def _run_question_listing(
    input_path: Path,
    *,
    docx_as_text: bool,
    show_ids: bool,
    show_meta: bool,
    show_eval: bool,
) -> None:
    if docx_as_text:
        print("[WARN] Question listing is unavailable when --docx-as-text is set.")
        return

    if input_path.suffix.lower() != ".docx":
        print("[WARN] Question listing currently supports DOCX files only.")
        return

    slots_payload = extract_slots_from_docx(str(input_path))
    slot_list = slots_payload.get("slots", [])
    if not slot_list:
        print("[INFO] No questions detected in the document.")
        return

    print(f"[INFO] Detected {len(slot_list)} questions:")
    print()
    details: List[Tuple[int, str, str]] = []
    question_blocks: set[int] = set()
    heuristic_blocks: set[int] = set()
    for i, slot in enumerate(slot_list, 1):
        q_text = (slot.get("question_text") or "").strip() or "[blank question text]"
        prefix = f"{slot.get('id')} - " if show_ids and slot.get("id") else ""
        print(f"  {i}. {prefix}{q_text}")
        if show_meta:
            print(json.dumps(slot, indent=2, ensure_ascii=False))
            print()

        detector = (slot.get("meta") or {}).get("detector", "unknown")
        locator = slot.get("answer_locator") or {}
        locator_type = locator.get("type", "unknown")
        if locator_type == "paragraph":
            loc_desc = f"paragraph index {locator.get('paragraph_index', '?')}"
        elif locator_type == "paragraph_after":
            loc_desc = (
                f"paragraph {locator.get('paragraph_index', '?')} + offset {locator.get('offset', '?')}"
            )
        elif locator_type == "table_cell":
            loc_desc = (
                f"table index {locator.get('table_index', '?')} (row {locator.get('row', '?')}, col {locator.get('col', '?')})"
            )
        else:
            loc_desc = json.dumps(locator)
        details.append((i, detector, loc_desc))

        q_block = (slot.get("meta") or {}).get("q_block")
        if q_block is not None:
            question_blocks.add(q_block)
            detector_tag = (slot.get("meta") or {}).get("detector")
            if detector_tag == "heuristic_promoted":
                heuristic_blocks.add(q_block)

    if details:
        print("\n[INFO] Detection details:")
        for idx, detector, loc_desc in details:
            print(f"  {idx}. detector={detector}; locator={loc_desc}")

    if show_eval:
        print("\n[INFO] Heuristic evaluation of paragraphs:")
        doc = docx.Document(str(input_path))
        block_items = list(_iter_block_items(doc))
        for idx, block in enumerate(block_items):
            if not isinstance(block, Paragraph):
                continue
            text = (block.text or "").strip()
            if not text:
                continue
            diag = _diagnose_paragraph(text)
            looks_like = bool(diag.get("looks_like"))
            ends_q = bool(diag.get("ends_with_q"))
            label: List[str] = []
            if idx in question_blocks:
                label.append("assigned" if idx not in heuristic_blocks else "assigned-heuristic")
            if looks_like and idx not in question_blocks:
                label.append("heuristic-match")
            if ends_q:
                label.append("ends-with-?")
            if not label:
                label.append("ignored")
            tags = ", ".join(label)
            preview = text[:160] + ("…" if len(text) > 160 else "")
            print(f"  [{idx}] {tags}: {preview}")
            if idx in heuristic_blocks:
                cues = diag.get("positives") or []
                if cues:
                    print(f"        ↳ cues (promoted heuristic): {', '.join(cues)}")
                else:
                    print("        ↳ cues (promoted heuristic): none recorded")
            elif idx in question_blocks:
                cues = diag.get("positives") or []
                if cues:
                    print(f"        ↳ cues: {', '.join(cues)}")
            elif looks_like:
                cues = diag.get("positives") or []
                if cues:
                    print(f"        ↳ cues (heuristic only): {', '.join(cues)}")
                else:
                    print("        ↳ cues (heuristic only): none recorded")
                print("        ↳ status: not extracted in slot list")
            else:
                reasons = diag.get("negatives") or []
                if reasons:
                    print(f"        ↳ rejected because: {', '.join(reasons)}")
                else:
                    print("        ↳ rejected because: no question cues detected")
# ---------------------------------------------------------------------------
# CLI command implementations
# ---------------------------------------------------------------------------


def run_question_mode(args: argparse.Namespace) -> None:
    set_debug(bool(getattr(args, "debug", False)) or ENV_DEBUG_DEFAULT)

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

    print("[INFO] Question mode configuration")
    print(f"       Framework: {framework}")
    print(f"       Model: {model}")
    print(f"       Search mode: {args.search_mode}")
    if args.fund:
        print(f"       Fund tag: {args.fund}")
    print(f"       Include citations: {'yes' if include_citations else 'no'}")
    if extra_docs:
        for doc in extra_docs:
            print(f"       Extra doc: {doc}")
    print()
    log_debug(
        "document_mode "
        f"input={input_path} output_dir={output_dir} framework={framework} model={model} "
        f"search_mode={args.search_mode} k={args.k} min_confidence={args.min_confidence} "
        f"include_citations={include_citations} docx_as_text={args.docx_as_text}"
    )
    log_debug(
        "question_mode "
        f"framework={framework} model={model} search_mode={args.search_mode} "
        f"k={args.k} min_confidence={args.min_confidence} include_citations={include_citations}"
    )

    def answer_once(question: str) -> None:
        if not question or not question.strip():
            log_debug("Empty question detected; skipping generator call")
            print("[WARN] Empty question provided; skipping.")
            return
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
    set_debug(bool(getattr(args, "debug", False)) or ENV_DEBUG_DEFAULT)

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    framework = args.framework or os.getenv("ANSWER_FRAMEWORK", "aladdin")
    model = args.model or (MODEL_OPTIONS[DEFAULT_INDEX] if MODEL_OPTIONS else DEFAULT_MODEL)
    llm = resolve_llm_client(framework, model)
    extra_docs = ensure_paths_exist(args.extra_doc)
    include_citations = args.include_citations
    list_only = bool(getattr(args, "list_questions", False))

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else input_path.parent

    print("[INFO] Document mode configuration")
    print(f"       Input: {input_path}")
    print(f"       Output directory: {output_dir}")
    print(f"       Framework: {framework}")
    print(f"       Model: {model}")
    print(f"       Search mode: {args.search_mode}")
    if args.fund:
        print(f"       Fund tag: {args.fund}")
    print(f"       Include citations: {'yes' if include_citations else 'no'}")
    print(f"       Max hits: {args.k}")
    print(f"       Answer length: {args.length}")
    if args.approx_words:
        print(f"       Approx words: {args.approx_words}")
    print(f"       Minimum confidence: {args.min_confidence}")
    if extra_docs:
        for doc in extra_docs:
            print(f"       Extra doc: {doc}")
    print()

    suffix = input_path.suffix.lower()
    if list_only:
        _run_question_listing(
            input_path,
            docx_as_text=args.docx_as_text,
            show_ids=False,
            show_meta=False,
            show_eval=bool(getattr(args, "show_eval", False)),
        )
        return

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
    skipped = result.get("skipped_slots")
    if skipped:
        print(
            f"[WARN] {len(skipped)} slots missing question text were skipped. "
            "Review the slots JSON for details."
        )


def run_questions_mode(args: argparse.Namespace) -> None:
    set_debug(bool(getattr(args, "debug", False)) or ENV_DEBUG_DEFAULT)

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    _run_question_listing(
        input_path,
        docx_as_text=False,
        show_ids=bool(getattr(args, "show_ids", False)),
        show_meta=bool(getattr(args, "show_meta", False)),
        show_eval=bool(getattr(args, "show_eval", False)),
    )

# ---------------------------------------------------------------------------
# Wizard helpers (interactive UX)
# ---------------------------------------------------------------------------


def _prompt_bool(message: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        response = input(f"{message}{suffix}: ").strip().lower()
        if not response:
            return default
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        print("Please answer 'y' or 'n'.")


def _prompt_text(message: str, default: Optional[str] = None, required: bool = False) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        value = input(f"{message}{suffix}: ").strip()
        if not value and default is not None:
            value = default
        if value or not required:
            return value
        print("This value is required.")


def _prompt_int(message: str, default: int, minimum: Optional[int] = None) -> int:
    suffix = f" [{default}]"
    while True:
        value = input(f"{message}{suffix}: ").strip()
        if not value:
            return default
        try:
            result = int(value)
        except ValueError:
            print("Enter a whole number.")
            continue
        if minimum is not None and result < minimum:
            print(f"Enter a value >= {minimum}.")
            continue
        return result


def _prompt_float(message: str, default: float, minimum: Optional[float] = None) -> float:
    suffix = f" [{default}]"
    while True:
        value = input(f"{message}{suffix}: ").strip()
        if not value:
            return default
        try:
            result = float(value)
        except ValueError:
            print("Enter a numeric value.")
            continue
        if minimum is not None and result < minimum:
            print(f"Enter a value >= {minimum}.")
            continue
        return result


def _prompt_optional_int(message: str, minimum: Optional[int] = None) -> Optional[int]:
    while True:
        value = input(f"{message}: ").strip()
        if not value:
            return None
        try:
            result = int(value)
        except ValueError:
            print("Enter a whole number or leave blank to skip.")
            continue
        if minimum is not None and result < minimum:
            print(f"Enter a value >= {minimum} or leave blank to skip.")
            continue
        return result


def _prompt_choice(
    message: str,
    options: Sequence[str],
    default_index: int = 0,
    formatter: Optional[Callable[[str], str]] = None,
) -> str:
    if not options:
        raise ValueError("No options provided")
    default_index = max(0, min(default_index, len(options) - 1))
    print(message)
    for idx, option in enumerate(options, start=1):
        label = formatter(option) if formatter else option
        default_mark = " *" if idx - 1 == default_index else ""
        print(f"  {idx}) {label}{default_mark}")
    while True:
        response = input(
            f"Select [1-{len(options)}] (Enter for default {default_index + 1}): "
        ).strip()
        if not response:
            return options[default_index]
        if response.isdigit():
            pos = int(response)
            if 1 <= pos <= len(options):
                return options[pos - 1]
        else:
            for option in options:
                label = formatter(option) if formatter else option
                if response.lower() in {option.lower(), label.lower()}:
                    return option
        print("Please select a valid option.")


def _prompt_paths(message: str) -> List[str]:
    response = input(f"{message} (comma separated, leave blank for none): ").strip()
    if not response:
        return []
    parts = [part.strip() for part in response.split(",") if part.strip()]
    return [str(Path(part).expanduser()) for part in parts]


def _fund_preview() -> None:
    tags = load_fund_tags()
    if tags:
        preview = ", ".join(tags[:10])
        more = "" if len(tags) <= 10 else " …"
        print(f"Known fund tags include: {preview}{more}")


def _wizard_question() -> None:
    print("\n--- Question Mode Wizard ---")
    _fund_preview()

    framework_default = os.getenv("ANSWER_FRAMEWORK", "aladdin").lower()
    framework_options = ["aladdin", "openai"]
    framework_index = 1 if framework_default == "openai" else 0
    framework = _prompt_choice("Choose framework", framework_options, framework_index)

    model_choices = list(MODEL_OPTIONS) or [DEFAULT_MODEL]
    try:
        model_index = model_choices.index(DEFAULT_MODEL)
    except ValueError:
        model_index = 0
    model = _prompt_choice(
        "Choose model",
        model_choices,
        model_index,
        formatter=lambda m: (
            f"{MODEL_SHORT_NAMES.get(m, m)} - {MODEL_DESCRIPTIONS.get(m, '')}"
        ).strip(" -"),
    )

    fund = _prompt_text("Fund tag (press Enter to skip)", default="")
    include_citations = _prompt_bool("Include citations in responses?", True)
    extra_docs = _prompt_paths("Additional documents to include")
    debug_flag = _prompt_bool("Enable verbose debug logging?", False)

    search_mode = "both"
    k = 20
    length = "long"
    approx_words = None
    min_confidence = 0.0

    if _prompt_bool("Adjust advanced retrieval settings?", False):
        search_mode = _prompt_text("Search mode", default=search_mode) or search_mode
        k = _prompt_int("Max hits per question", k, minimum=1)
        length = _prompt_choice(
            "Answer length",
            ["auto", "short", "medium", "long"],
            ["auto", "short", "medium", "long"].index(length),
        )
        approx_words = _prompt_optional_int(
            "Approximate words per answer (leave blank to skip)",
            minimum=1,
        )
        min_confidence = _prompt_float(
            "Minimum confidence score",
            min_confidence,
            minimum=0.0,
        )

    if _prompt_bool("Provide a question now? (No opens interactive chat)", True):
        question = _prompt_text("Enter your question", required=True)
    else:
        question = ""

    response_mode = _prompt_choice(
        "Response style",
        ["generate", "preapproved"],
        0,
        formatter=lambda mode: (
            "Generate new answers" if mode == "generate" else "Closest pre-approved answers"
        ),
    )

    namespace = argparse.Namespace(
        command="ask",
        fund=fund or None,
        framework=framework,
        model=model,
        search_mode=search_mode,
        k=k,
        min_confidence=min_confidence,
        length=length,
        approx_words=approx_words,
        include_citations=include_citations,
        extra_doc=extra_docs or None,
        question=question or None,
        response_mode=response_mode,
        debug=debug_flag,
    )

    try:
        run_question_mode(namespace)
    except Exception as exc:  # pragma: no cover - interactive convenience
        print(f"[ERROR] {exc}")


def _wizard_document() -> None:
    print("\n--- Document Mode Wizard ---")

    while True:
        raw_path = _prompt_text("Path to document", required=True)
        input_path = Path(raw_path).expanduser()
        if input_path.exists():
            break
        print(f"File not found: {input_path}")

    suffix = input_path.suffix.lower()

    _fund_preview()
    fund = _prompt_text("Fund tag (press Enter to skip)", default="")
    include_citations = _prompt_bool("Include citations in responses?", True)
    extra_docs = _prompt_paths("Additional documents to include")
    output_dir = _prompt_text(
        "Output directory (press Enter to use document folder)",
        default="",
    )
    debug_flag = _prompt_bool("Enable verbose debug logging?", False)

    framework_default = os.getenv("ANSWER_FRAMEWORK", "aladdin").lower()
    framework_options = ["aladdin", "openai"]
    framework_index = 1 if framework_default == "openai" else 0
    framework = _prompt_choice("Choose framework", framework_options, framework_index)

    model_choices = list(MODEL_OPTIONS) or [DEFAULT_MODEL]
    try:
        model_index = model_choices.index(DEFAULT_MODEL)
    except ValueError:
        model_index = 0
    model = _prompt_choice(
        "Choose model",
        model_choices,
        model_index,
        formatter=lambda m: (
            f"{MODEL_SHORT_NAMES.get(m, m)} - {MODEL_DESCRIPTIONS.get(m, '')}"
        ).strip(" -"),
    )

    search_mode = "both"
    k = 20
    length = "long"
    approx_words = None
    min_confidence = 0.0

    if _prompt_bool("Adjust advanced retrieval settings?", False):
        search_mode = _prompt_text("Search mode", default=search_mode) or search_mode
        k = _prompt_int("Max hits per question", k, minimum=1)
        length = _prompt_choice(
            "Answer length",
            ["auto", "short", "medium", "long"],
            ["auto", "short", "medium", "long"].index(length),
        )
        approx_words = _prompt_optional_int(
            "Approximate words per answer (leave blank to skip)",
            minimum=1,
        )
        min_confidence = _prompt_float(
            "Minimum confidence score",
            min_confidence,
            minimum=0.0,
        )

    docx_as_text = False
    docx_write_mode = "fill"
    if suffix == ".docx":
        docx_as_text = _prompt_bool("Treat DOCX as plain text?", False)
        if not docx_as_text:
            docx_write_mode = _prompt_choice(
                "DOCX write mode",
                ["fill", "replace", "append"],
                0,
                formatter=lambda mode: {
                    "fill": "Fill empty slots",
                    "replace": "Overwrite existing answers",
                    "append": "Append to existing content",
                }[mode],
            )

    show_live = _prompt_bool("Show each question/answer while processing?", False)

    namespace = argparse.Namespace(
        command="document",
        input=str(input_path),
        fund=fund or None,
        framework=framework,
        model=model,
        search_mode=search_mode,
        k=k,
        min_confidence=min_confidence,
        length=length,
        approx_words=approx_words,
        include_citations=include_citations,
        extra_doc=extra_docs or None,
        output_dir=output_dir or None,
        docx_as_text=docx_as_text,
        docx_write_mode=docx_write_mode,
        show_live=show_live,
        debug=debug_flag,
    )

    try:
        run_document_mode(namespace)
    except Exception as exc:  # pragma: no cover - interactive convenience
        print(f"[ERROR] {exc}")


def run_wizard() -> None:
    print("RFP Responder CLI Wizard")
    print("==========================")
    print("This guided mode mirrors the Streamlit UI with simple prompts.")

    while True:
        print("\nWhat would you like to do?")
        print("  1) Process an RFP document")
        print("  2) Ask questions or chat")
        print("  3) Quit")
        choice = input("Select an option [1-3]: ").strip().lower()

        if choice in {"1", "doc", "document"}:
            _wizard_document()
        elif choice in {"2", "ask", "chat"}:
            _wizard_question()
        elif choice in {"3", "q", "quit", "exit"}:
            print("Goodbye!")
            return
        else:
            print("Please choose 1, 2, or 3.")

        input("\nPress Enter to return to the menu...")


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
    common.add_argument(
        "--debug",
        action="store_true",
        help="Print verbose debug logs to help troubleshoot",
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
    doc.add_argument(
        "--show-eval",
        action="store_true",
        help="Also display heuristic evaluation when listing questions",
    )
    doc.add_argument(
        "--list-questions",
        action="store_true",
        help="For DOCX inputs, list detected questions and exit",
    )

    questions = subparsers.add_parser(
        "questions",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        help="List detected questions in a DOCX template",
    )
    questions.add_argument("input", help="Path to the DOCX file to inspect")
    questions.add_argument("--show-ids", action="store_true", help="Include slot identifiers in output")
    questions.add_argument("--show-meta", action="store_true", help="Print full slot metadata JSON")
    questions.add_argument(
        "--show-eval",
        action="store_true",
        help="Show heuristic evaluation for non-question paragraphs",
    )
    questions.add_argument("--debug", action="store_true", help="Print verbose debug logs")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    if not argv or "--wizard" in argv:
        run_wizard()
        return

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ask":
        run_question_mode(args)
    elif args.command == "document":
        run_document_mode(args)
    elif args.command == "questions":
        run_questions_mode(args)
    else:  # pragma: no cover - defensive
        parser.error("Unknown command")


if __name__ == "__main__":
    main()
