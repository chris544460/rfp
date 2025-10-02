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
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from textwrap import dedent
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
import docx
from docx.table import Table
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
    _looks_like_question,
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


def _resolve_concurrency(value: Optional[int]) -> int:
    env = os.getenv("CLI_STREAMLIT_CONCURRENCY")
    resolved = value
    if resolved is None and env:
        try:
            resolved = int(env)
        except ValueError:
            print(f"[WARN] Invalid CLI_STREAMLIT_CONCURRENCY '{env}'; falling back to default")
    if resolved is None:
        cpu_default = max(1, (os.cpu_count() or 4))
        resolved = min(8, max(2, cpu_default))
    return max(1, resolved)


class StageTimer:
    """Collect fine-grained timing data for CLI workflows."""

    def __init__(self) -> None:
        self._entries: List[Dict[str, object]] = []

    def track(self, name: str, *, meta: Optional[Dict[str, object]] = None):
        return _StageTimerContext(self, name, meta)

    def add(self, name: str, seconds: float, *, meta: Optional[Dict[str, object]] = None) -> None:
        self._entries.append({"name": name, "seconds": seconds, "meta": meta})

    def breakdown(self) -> List[Dict[str, object]]:
        totals: Dict[str, Dict[str, object]] = defaultdict(lambda: {"name": "", "count": 0, "total": 0.0})
        for entry in self._entries:
            slot = totals[entry["name"]]
            if not slot["name"]:
                slot["name"] = entry["name"]
            slot["count"] += 1
            slot["total"] += float(entry["seconds"])
        results: List[Dict[str, object]] = []
        for slot in totals.values():
            count = slot["count"] or 1
            slot["avg"] = slot["total"] / count
            results.append(slot)
        return sorted(results, key=lambda item: item["total"], reverse=True)

    def records_for(self, name: str) -> List[Dict[str, object]]:
        return [entry for entry in self._entries if entry["name"] == name]


class _StageTimerContext:
    def __init__(self, timer: StageTimer, name: str, meta: Optional[Dict[str, object]]) -> None:
        self._timer = timer
        self._name = name
        self._meta = meta
        self._start: Optional[float] = None

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        end = time.perf_counter()
        if self._start is None:
            return
        self._timer.add(self._name, end - self._start, meta=self._meta)


def _emit_timing_report(timer: StageTimer, total_duration: float) -> None:
    breakdown = timer.breakdown()
    if not breakdown:
        return

    print("\n[INFO] Stage timing breakdown:")
    measured_total = 0.0
    for entry in breakdown:
        name = entry["name"]
        count = entry["count"]
        total = entry["total"]
        avg = entry["avg"]
        measured_total += total
        print(f"  {name}: {count} call(s), total {total:.2f}s, avg {avg:.2f}s")

    docx_answers = timer.records_for("docx:answer_generation")
    if docx_answers:
        slowest = sorted(docx_answers, key=lambda rec: rec["seconds"], reverse=True)[:5]
        print("  Slowest DOCX answer generations:")
        for rec in slowest:
            meta = rec.get("meta") or {}
            idx = meta.get("slot_index")
            slot_id = meta.get("slot_id")
            preview = meta.get("question_preview") or ""
            if preview and len(preview) > 75:
                preview = preview[:72] + "…"
            label = f"slot {idx}" if idx is not None else "slot ?"
            if slot_id:
                label += f" ({slot_id})"
            print(f"    {label}: {rec['seconds']:.2f}s | {preview}")

    print(f"  Measured total: {measured_total:.2f}s of {total_duration:.2f}s overall.")


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
DOC_DEFAULT_MODEL = "o3-2025-04-16_research"


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
    *,
    timer: Optional[StageTimer] = None,
    concurrency: Optional[int] = None,
) -> Dict[str, object]:
    timer = timer or StageTimer()
    print("[INFO] Reading Excel workbook …")
    with timer.track("excel:collect_cells"):
        collect_non_empty_cells(str(input_path))
    with timer.track("excel:detect_schema"):
        schema = ask_sheet_schema(str(input_path))

    total_questions = len(schema)
    answers: List[object] = [""] * total_questions
    worker_limit = max(1, min(concurrency or 1, total_questions) if total_questions else 1)

    def _generate_excel_answer(question: str):
        start = time.perf_counter()
        result = generator(question)
        duration = time.perf_counter() - start
        return result, duration

    futures = {}
    with ThreadPoolExecutor(max_workers=worker_limit) as pool:
        for idx, entry in enumerate(schema, start=1):
            question = (entry.get("question_text") or "").strip()
            if show_live and question:
                print(f"[Q{idx}] {question}")
            future = pool.submit(_generate_excel_answer, question)
            futures[future] = {
                "row_index": idx,
                "list_index": idx - 1,
                "question_preview": question[:80],
            }

        completed = 0
        scheduled = len(futures)
        for future in as_completed(futures):
            meta = futures[future]
            try:
                answer, duration = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                raise RuntimeError(
                    f"Failed to generate answer for Excel row {meta.get('row_index')}: {exc}"
                ) from exc
            answers[meta["list_index"]] = answer
            timer.add("excel:answer_generation", duration, meta=meta)
            completed += 1
            if show_live:
                text = answer.get("text", "") if isinstance(answer, dict) else answer
                print(f"  ↳ {text}\n")
            else:
                print(
                    f"[INFO] Answered {completed}/{scheduled}",
                    end="\r",
                    flush=True,
                )

    if not show_live and futures:
        print()
    else:
        print()

    with timer.track("excel:prepare_output_dir"):
        output_dir.mkdir(parents=True, exist_ok=True)
    answered_path = output_dir / f"{input_path.stem}_answered.xlsx"
    with timer.track("excel:write_answers"):
        write_excel_answers(
            schema,
            answers,
            str(input_path),
            str(answered_path),
            include_comments=include_citations,
        )
    comments_path = answered_path.with_name(answered_path.stem + "_comments.docx")

    qa_pairs = []
    with timer.track("excel:build_qa_pairs"):
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
    *,
    timer: Optional[StageTimer] = None,
    concurrency: Optional[int] = None,
) -> Dict[str, object]:
    timer = timer or StageTimer()
    print("[INFO] Extracting slots from DOCX …")
    with timer.track("docx:extract_slots"):
        slots_payload = extract_slots_from_docx(str(input_path))
    slots = slots_payload.get("slots", [])
    answers_by_id: Dict[str, object] = {}
    total_slots = len(slots)
    skipped_slots: List[str] = []
    worker_limit = max(1, min(concurrency or 1, total_slots) if total_slots else 1)
    futures = {}

    def _generate_slot_answer(question: str):
        start = time.perf_counter()
        result = generator(question)
        duration = time.perf_counter() - start
        return result, duration

    with ThreadPoolExecutor(max_workers=worker_limit) as pool:
        for idx, slot in enumerate(slots, start=1):
            question = (slot.get("question_text") or "").strip()
            slot_id = slot.get("id", f"slot_{idx}")
            if not question:
                with timer.track(
                    "docx:slot_skipped",
                    meta={"slot_index": idx, "slot_id": slot_id},
                ):
                    log_debug(
                        f"DOCX slot {slot_id} (index {idx}) had empty question text; storing blank answer"
                    )
                    if not show_live:
                        print(f"[WARN] Slot {idx} has no extracted question; skipping.")
                    if show_live:
                        print(f"[Slot {idx}] (no question detected; skipped)")
                    answers_by_id[slot_id] = ""
                    skipped_slots.append(slot_id)
                continue
            if show_live:
                print(f"[Slot {idx}] {question}")
            meta = {
                "slot_index": idx,
                "slot_id": slot_id,
                "question_preview": question[:80],
            }
            future = pool.submit(_generate_slot_answer, question)
            futures[future] = meta

        completed = 0
        scheduled = len(futures)
        for future in as_completed(futures):
            meta = futures[future]
            try:
                answer, duration = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                raise RuntimeError(
                    f"Failed to generate answer for slot {meta.get('slot_id')}: {exc}"
                ) from exc
            timer.add("docx:answer_generation", duration, meta=meta)
            answers_by_id[meta["slot_id"]] = answer
            completed += 1
            if show_live:
                text = answer.get("text", "") if isinstance(answer, dict) else answer
                print(f"  ↳ {text}\n")
            else:
                print(
                    f"[INFO] Answered {completed}/{scheduled}",
                    end="\r",
                    flush=True,
                )

    if not show_live and futures:
        print()
    else:
        print()

    with timer.track("docx:prepare_output_dir"):
        output_dir.mkdir(parents=True, exist_ok=True)
    answered_path = output_dir / f"{input_path.stem}_answered.docx"
    slots_tmp = output_dir / f"{input_path.stem}_slots.json"
    answers_tmp = output_dir / f"{input_path.stem}_answers.json"

    with timer.track("docx:write_intermediate_json"):
        slots_tmp.write_text(json.dumps(slots_payload), encoding="utf-8")
        answers_tmp.write_text(json.dumps({"by_id": answers_by_id}), encoding="utf-8")

    with timer.track("docx:apply_answers"):
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
    with timer.track("docx:build_qa_pairs"):
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
    *,
    timer: Optional[StageTimer] = None,
    concurrency: Optional[int] = None,
) -> Dict[str, object]:
    timer = timer or StageTimer()
    print("[INFO] Loading document text …")
    with timer.track("text:load_input"):
        raw_text = load_input_text(str(input_path))
    with timer.track("text:extract_questions"):
        questions = extract_questions(raw_text, llm)

    if not questions:
        print("[WARN] No questions could be extracted from the document.")
        return {
            "mode": "document_summary",
            "output_file": None,
            "qa_pairs": [],
        }

    processed_questions: List[str] = []
    question_indices: List[int] = []
    for idx, question in enumerate(questions, start=1):
        stripped = question.strip()
        if not stripped:
            log_debug(f"Text mode question index {idx} was empty; skipping")
            continue
        processed_questions.append(stripped)
        question_indices.append(idx)

    total = len(processed_questions)
    answers: List[object] = [""] * total
    comments: List[List[Tuple[str, str, str, float, str]]] = [
        [] for _ in range(total)
    ]
    worker_limit = max(1, min(concurrency or 1, total) if total else 1)

    def _generate_text_answer(question: str):
        start = time.perf_counter()
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
        duration = time.perf_counter() - start
        return answer, cmts, duration

    futures = {}
    with ThreadPoolExecutor(max_workers=worker_limit) as pool:
        for idx, question in enumerate(processed_questions):
            display_idx = question_indices[idx]
            if show_live:
                print(f"[Q{display_idx}] {question}")
            future = pool.submit(_generate_text_answer, question)
            futures[future] = {
                "question_index": display_idx,
                "list_index": idx,
                "question_preview": question[:80],
            }

        completed = 0
        scheduled = len(futures)
        for future in as_completed(futures):
            meta = futures[future]
            try:
                answer, cmts, duration = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                raise RuntimeError(
                    f"Failed to generate answer for question {meta.get('question_index')}: {exc}"
                ) from exc
            if not include_citations:
                answer = re.sub(r"\[\d+\]", "", answer)
                cmts = []
            answers[meta["list_index"]] = answer
            comments[meta["list_index"]] = cmts
            timer.add("text:answer_generation", duration, meta=meta)
            completed += 1
            if show_live:
                print(f"  ↳ {answer}\n")
            else:
                print(
                    f"[INFO] Answered {completed}/{scheduled}",
                    end="\r",
                    flush=True,
                )

    if not show_live:
        print()
    elif futures:
        print()

    with timer.track("text:prepare_output_dir"):
        output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{input_path.stem}_answered.docx"
    with timer.track("text:build_doc"):
        report_bytes = build_docx(
            processed_questions,
            answers,
            comments,
            include_comments=include_citations,
        )
    with timer.track("text:write_output"):
        output_path.write_bytes(report_bytes)

    qa_pairs = []
    with timer.track("text:build_qa_pairs"):
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
    skipped_entries = slots_payload.get("skipped_slots", [])
    heuristic_entries = slots_payload.get("heuristic_skips", [])
    if not slot_list and not skipped_entries and not heuristic_entries:
        print("[INFO] No questions detected in the document.")
        return

    print(f"[INFO] Detected {len(slot_list)} question(s):")
    print()

    def _normalize_question_text(text: str) -> str:
        return strip_enum_prefix((text or "").strip()).lower()

    details: List[Tuple[int, str, str]] = []
    question_blocks: set[int] = set()
    heuristic_blocks: set[int] = set()
    skipped_blocks: set[int] = set()
    skipped_reason_by_block: Dict[int, str] = {}
    slot_lookup: Dict[str, List[Tuple[int, Optional[int], str]]] = {}
    for i, slot in enumerate(slot_list, 1):
        q_text = (slot.get("question_text") or "").strip() or "[blank question text]"
        prefix = f"{slot.get('id')} - " if show_ids and slot.get("id") else ""
        print(f"  {i}. {prefix}{q_text}")
        if show_meta:
            print(json.dumps(slot, indent=2, ensure_ascii=False))
            print()

        slot_meta = (slot.get("meta") or {})
        detector = slot_meta.get("detector", "unknown")
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

        q_block = slot_meta.get("q_block")
        if q_block is not None:
            question_blocks.add(q_block)
            if slot_meta.get("detector") == "heuristic_promoted":
                heuristic_blocks.add(q_block)

        norm = _normalize_question_text(q_text)
        if norm:
            slot_lookup.setdefault(norm, []).append((i, q_block, detector))

    combined_skips: List[Dict[str, object]] = []
    if skipped_entries:
        for entry in skipped_entries:
            combined_skips.append(
                {
                    "question_text": (entry.get("question_text") or "").strip() or "[blank question text]",
                    "reason_key": (entry.get("reason") or "").strip() or "unspecified",
                    "source": "slot_filter",
                    "paragraph_index": (entry.get("meta") or {}).get("q_block"),
                }
            )
    if heuristic_entries:
        for entry in heuristic_entries:
            combined_skips.append(
                {
                    "question_text": (entry.get("question_text") or "").strip() or "[blank question text]",
                    "reason": entry.get("reason", "unspecified"),
                    "source": "heuristic",
                    "paragraph_index": entry.get("paragraph_index"),
                }
            )

    if combined_skips:
        print()
        print(f"[INFO] Skipped {len(combined_skips)} question(s):")
        for entry in combined_skips:
            q_text = entry["question_text"]
            if entry["source"] == "slot_filter":
                reason_label = {
                    "table_reference": "mentions a table; tables are not supported yet",
                    "blank_question_text": "blank question text",
                    "heuristic_veto": "failed question heuristics",
                    "llm_veto": "LLM candidate rejected by heuristics",
                }.get(entry["reason_key"], entry["reason_key"] or "unspecified")
            else:
                reason_label = entry.get("reason", "unspecified")
            print(f"  - {q_text} [{reason_label}]")
            qb = entry.get("paragraph_index")
            if isinstance(qb, int):
                if entry["source"] == "slot_filter":
                    question_blocks.add(qb)
                skipped_blocks.add(qb)
                skipped_reason_by_block[qb] = reason_label
                if entry["source"] == "heuristic":
                    heuristic_blocks.add(qb)

    doc = docx.Document(str(input_path))
    block_items = list(_iter_block_items(doc))
    heuristic_reasons: Dict[int, str] = {
        entry.get("paragraph_index"): entry.get("reason", "")
        for entry in heuristic_entries
        if isinstance(entry.get("paragraph_index"), int)
    }
    seen_norms: Set[str] = set()
    for idx, block in enumerate(block_items):
        if not isinstance(block, Paragraph):
            continue
        text = (block.text or "").strip()
        if not text:
            continue
        if idx in question_blocks:
            continue
        diag = _diagnose_paragraph(text)
        if not diag.get("looks_like"):
            continue
        norm = _normalize_question_text(text)
        prev_seen = bool(norm and norm in seen_norms)
        if norm:
            seen_norms.add(norm)
        slot_hits = slot_lookup.get(norm, []) if norm else []
        if idx in heuristic_reasons:
            reason = heuristic_reasons[idx]
        else:
            if slot_hits:
                slot_ids = ", ".join(str(hit[0]) for hit in slot_hits)
                detectors = {hit[2] for hit in slot_hits if hit[2] and hit[2] != "unknown"}
                if any(hit[1] is None for hit in slot_hits):
                    detector_note = f" ({', '.join(sorted(detectors))})" if detectors else ""
                    reason = (
                        "question text matches slot(s) "
                        f"{slot_ids} but extractor did not record a paragraph index{detector_note}"
                    )
                else:
                    reason = f"question text already covered by slot(s) {slot_ids}"
            else:
                factors: List[str] = []
                if prev_seen:
                    factors.append("duplicate question text encountered earlier in the document")
                next_block = block_items[idx + 1] if idx + 1 < len(block_items) else None
                if isinstance(next_block, Table):
                    factors.append(
                        "the next block is a table; automatic slot insertion inside tables is disabled"
                    )
                elif isinstance(next_block, Paragraph):
                    next_text = (next_block.text or "").strip()
                    if next_text and not _looks_like_question(next_text):
                        factors.append(
                            "the following paragraph already contains answer text and cannot be overwritten automatically"
                        )
                    elif not next_text:
                        factors.append(
                            "a blank paragraph follows, but the extractor still declined to insert due to prior safeguards"
                        )
                else:
                    if next_block is None:
                        factors.append("question appears at the end of the document")
                if not factors:
                    factors.append(
                        "heuristics saw a question, but no safe answer location was identified"
                    )
                reason = "; ".join(factors) + "; the extractor skipped slot creation"
            heuristic_reasons[idx] = reason

    if details:
        print("\n[INFO] Detection details:")
        for idx, detector, loc_desc in details:
            print(f"  {idx}. detector={detector}; locator={loc_desc}")

    if show_eval:
        print("\n[INFO] Heuristic evaluation of paragraphs:")
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
                if idx in skipped_blocks:
                    label.append("skipped")
                else:
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
            if idx in skipped_blocks:
                reason_key = skipped_reason_by_block.get(idx, "")
                reason_label = {
                    "table_reference": "mentions a table; tables are not supported yet",
                    "blank_question_text": "blank question text",
                    "heuristic_veto": "failed question heuristics",
                    "llm_veto": "LLM candidate rejected by heuristics",
                }.get(reason_key, reason_key or "unspecified")
                print(f"        ↳ skipped: {reason_label}")
            elif idx in heuristic_blocks:
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
                reason = heuristic_reasons.get(idx)
                if reason:
                    print(f"        ↳ status: not extracted in slot list ({reason})")
                else:
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
    available_models = MODEL_OPTIONS or (DOC_DEFAULT_MODEL,)
    model = args.model or DOC_DEFAULT_MODEL
    if model not in available_models:
        model = DOC_DEFAULT_MODEL if DOC_DEFAULT_MODEL in available_models else available_models[0]
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

    start_time = time.perf_counter()
    if args.question:
        answer_once(args.question)
        duration = time.perf_counter() - start_time
        print(f"\n[INFO] Completed in {duration:.2f} seconds.")
        return

    print("[INFO] Enter questions (Ctrl-D to exit).")
    try:
        while True:
            prompt = input("Question> ").strip()
            if not prompt:
                continue
            loop_start = time.perf_counter()
            answer_once(prompt)
            loop_duration = time.perf_counter() - loop_start
            print(f"[INFO] Response generated in {loop_duration:.2f} seconds.\n")
    except (EOFError, KeyboardInterrupt):
        total_duration = time.perf_counter() - start_time
        print(f"\n[INFO] Exiting question mode. Session length: {total_duration:.2f} seconds.")


def run_document_mode(args: argparse.Namespace) -> None:
    set_debug(bool(getattr(args, "debug", False)) or ENV_DEBUG_DEFAULT)

    timer = StageTimer()

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    framework = args.framework or os.getenv("ANSWER_FRAMEWORK", "aladdin")
    available_models = MODEL_OPTIONS or (DOC_DEFAULT_MODEL,)
    model = args.model or DOC_DEFAULT_MODEL
    if model not in available_models:
        model = DOC_DEFAULT_MODEL if DOC_DEFAULT_MODEL in available_models else available_models[0]
    llm = resolve_llm_client(framework, model)
    extra_docs = ensure_paths_exist(args.extra_doc)
    include_citations = args.include_citations
    list_only = bool(getattr(args, "list_questions", False))

    concurrency = _resolve_concurrency(getattr(args, "concurrency", None))

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else input_path.parent

    print("[INFO] Document mode configuration")
    print(f"       Input: {input_path}")
    print(f"       Output directory: {output_dir}")
    print(f"       Framework: {framework}")
    print(f"       Model: {model}")
    print(f"       Search mode: {args.search_mode}")
    print(f"       Concurrency: {concurrency}")
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
    start_time = time.perf_counter()
    if list_only:
        with timer.track("docx:list_questions"):
            _run_question_listing(
                input_path,
                docx_as_text=args.docx_as_text,
                show_ids=False,
                show_meta=False,
                show_eval=bool(getattr(args, "show_eval", False)),
            )
        duration = time.perf_counter() - start_time
        print(f"\n[INFO] Completed in {duration:.2f} seconds.")
        _emit_timing_report(timer, duration)
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
        result = process_excel(
            input_path,
            output_dir,
            generator,
            include_citations,
            args.show_live,
            timer=timer,
            concurrency=concurrency,
        )
    elif suffix == ".docx" and not args.docx_as_text:
        result = process_docx_slots(
            input_path,
            output_dir,
            generator,
            include_citations,
            args.docx_write_mode,
            args.show_live,
            timer=timer,
            concurrency=concurrency,
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
            timer=timer,
            concurrency=concurrency,
        )

    duration = time.perf_counter() - start_time
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
    print(f"[INFO] Completed in {duration:.2f} seconds.")
    _emit_timing_report(timer, duration)


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
    doc.add_argument(
        "--concurrency",
        type=int,
        help="Maximum number of questions to answer in parallel",
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
