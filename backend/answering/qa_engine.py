#!/usr/bin/env python3
"""
qa_core.py
Home of `answer_question(...)` and its prompt plumbing.

This module centralizes the RAG→LLM answer generation so both the CLI and
other pipelines can reuse it without circular imports.  Higher-level wrappers
such as `backend.answering.responder.Responder` and the Streamlit UI call into
these helpers to keep retrieval, filtering, and citation handling consistent.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable, Set, NamedTuple

# Retrieve context either from vector indexes or uploaded docs.
from backend.retrieval.vector_search import search

try:
    from backend.retrieval.document_search import search_uploaded_docs
except ModuleNotFoundError:  # pragma: no cover - optional docx dependency
    # Streamlit deployments without python-docx skip the LLM-powered doc search path.
    def search_uploaded_docs(*args, **kwargs):  # type: ignore[no-redef]
        return []

# Shared LLM client helpers and prompt loading utilities.
from backend.llm.completions_client import CompletionsClient
from backend.prompts import load_prompts


# Default debug flag; defaults to True unless explicitly disabled via env.
DEBUG = os.getenv("RFP_QA_DEBUG", "1").lower() not in {"", "0", "false"}

# Retry the model if we detect citation markers but end up with no comments.
# Can be overridden via the RFP_COMMENT_RETRIES environment variable.
MAX_COMMENT_RETRIES = int(os.getenv("RFP_COMMENT_RETRIES", "2"))

# Regex for [1] or comma-separated citations like [1, 2]
CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


# ───────────────────────── Prompt loading ─────────────────────────

# Prompt templates are cached here so every call shares the same in-memory copy.
PROMPTS = load_prompts(
    {name: "" for name in ("extract_questions", "answer_search_context", "answer_llm")}
)

PRESET_INSTRUCTIONS: Dict[str, str] = {
    "short": "Answer briefly in 1–2 sentences.",
    "medium": "Answer in one concise paragraph.",
    "long": "Answer in detail (up to one page).",
    "auto": "Answer using only the provided sources and choose an appropriate length.",
}


# ───────────────────────── Core answering ─────────────────────────


def _gather_vector_hits(
    query: str,
    *,
    mode: str,
    fund: Optional[str],
    k: int,
    include_vectors: bool,
) -> List[Dict[str, object]]:
    """Run the vector index searches according to the requested mode."""
    if mode != "both":
        return search(
            query,
            k=k,
            mode=mode,
            fund_filter=fund,
            include_vectors=include_vectors,
        )

    per_mode_k = max(1, k)
    hits: List[Dict[str, object]] = []
    try:
        hits.extend(
            search(
                query,
                k=per_mode_k,
                mode="blend",
                fund_filter=fund,
                include_vectors=include_vectors,
            )
        )
    except AssertionError:
        if DEBUG:
            print("[qa_core] blend index unavailable; skipping blend search")

    for specialized_mode in ("question", "answer"):
        hits.extend(
            search(
                query,
                k=per_mode_k,
                mode=specialized_mode,
                fund_filter=fund,
                include_vectors=include_vectors,
            )
        )
    return hits


def _extend_with_uploaded_docs(
    hits: List[Dict[str, object]],
    *,
    query: str,
    extra_docs: Optional[List[str]],
    llm: CompletionsClient,
) -> None:
    """Augment vector hits with snippets from uploaded documents."""
    if not extra_docs:
        return
    if DEBUG:
        print(f"[qa_core] LLM searching {len(extra_docs)} uploaded docs")
    hits.extend(search_uploaded_docs(query, extra_docs, llm))


def _log_candidate_hits(hits: List[Dict[str, object]], *, k: int) -> None:
    """Emit verbose diagnostics for the strongest matches."""
    if not DEBUG or not hits:
        return
    print(f"[qa_core] retrieved {len(hits)} hits before filtering")
    top_n = min(len(hits), k)
    print(f"[qa_core] top {top_n} hits:")
    for i, hit in enumerate(hits[:top_n], 1):
        meta = hit.get("meta", {}) or {}
        src = meta.get("source", "unknown")
        doc_id = hit.get("id", "unknown")
        score = float(hit.get("cosine", 0.0))
        snippet = (hit.get("text") or "").strip().replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        print(f"    {i}. id={doc_id} score={score:.3f} source={src} text='{snippet}'")


def _append_diagnostic_entry(
    bucket: List[Dict[str, object]],
    *,
    hit: Dict[str, object],
    status: str,
    reason: str,
    label: Optional[str],
    raw_rank: int,
    score: float,
    src_origin: str,
    src_path: str,
    src_name: str,
    date_str: str,
    include_vectors: bool,
) -> None:
    """Collect rich debugging info for inspection in Streamlit's diagnostics sidebar."""
    entry: Dict[str, object] = {
        "raw_rank": raw_rank,
        "id": hit.get("id", "unknown"),
        "score": score,
        "origin": src_origin,
        "status": status,
        "reason": reason,
        "snippet": (hit.get("text") or "").strip(),
        "source_path": src_path,
        "source_name": src_name,
        "date": date_str,
    }
    if label is not None:
        entry["label"] = label
    if include_vectors and "embedding" in hit:
        entry["embedding"] = hit.get("embedding")
    if include_vectors and "embedding_error" in hit:
        entry["embedding_error"] = hit.get("embedding_error")
    if "raw_index" in hit:
        entry["raw_index"] = hit.get("raw_index")
    bucket.append(entry)


class HitContext(NamedTuple):
    hit: Dict[str, object]
    doc_id: str
    score: float
    snippet: str
    src_origin: str
    src_path: str
    src_name: str
    date_str: str


def _build_hit_context(hit: Dict[str, object]) -> HitContext:
    """Normalize search hit fields so downstream filters can treat sources uniformly."""
    doc_id = str(hit.get("id", "unknown"))
    score = float(hit.get("cosine", 0.0))
    snippet = (hit.get("text") or "").strip()
    src_origin = str(hit.get("origin") or "unknown")
    meta = hit.get("meta", {}) or {}
    src_path = str(meta.get("source", "")) or "unknown"
    src_name = Path(src_path).name if src_path else "unknown"
    try:
        mtime = (
            Path(src_path).stat().st_mtime
            if src_path and Path(src_path).exists()
            else None
        )
        date_str = (
            datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            if mtime
            else "unknown"
        )
    except Exception:
        date_str = "unknown"
    return HitContext(
        hit=hit,
        doc_id=doc_id,
        score=score,
        snippet=snippet,
        src_origin=src_origin,
        src_path=src_path,
        src_name=src_name,
        date_str=date_str,
    )


def _classify_hit(
    ctx: HitContext,
    min_confidence: float,
    seen_snippets: Set[str],
) -> Tuple[str, str]:
    """Return (decision, reason) for a hit based on score thresholds and duplication."""
    if ctx.score < min_confidence:
        return (
            "low_confidence",
            f"score {ctx.score:.3f} < min_confidence {min_confidence:.3f}",
        )
    if not ctx.snippet:
        return "empty", "empty snippet"
    if ctx.snippet in seen_snippets:
        return "duplicate", "duplicate snippet"
    return "accepted", ""


def _status_for_decision(decision: str) -> str:
    """Map internal decision codes to the terms surfaced in diagnostics."""
    return {
        "low_confidence": "filtered_low_confidence",
        "duplicate": "filtered_duplicate",
        "empty": "filtered_empty",
    }.get(decision, "accepted")


def _debug_filter_message(decision: str, ctx: HitContext, reason: str) -> None:
    """Emit debug noise explaining why a snippet was kept or filtered."""
    if not DEBUG:
        return
    if decision == "accepted":
        print(
            f"[qa_core] accepted snippet {ctx.doc_id} from {ctx.src_name} score={ctx.score:.3f}"
        )
        return
    print(f"[qa_core] filter out id={ctx.doc_id} {reason}")


def _filter_hits(
    hits: List[Dict[str, object]],
    *,
    min_confidence: float,
    diagnostics: Optional[List[Dict[str, object]]],
    include_vectors: bool,
) -> Tuple[List[Tuple[str, str, str, float, str]], Dict[str, int]]:
    """Apply confidence/duplication rules and build the context rows."""
    seen_snippets: Set[str] = set()
    rows: List[Tuple[str, str, str, float, str]] = []
    stats = {"accepted": 0, "low_confidence": 0, "duplicates": 0, "empties": 0}

    for raw_rank, hit in enumerate(hits, 1):
        ctx = _build_hit_context(hit)
        decision, reason = _classify_hit(ctx, min_confidence, seen_snippets)

        if decision == "accepted":
            label = f"[{len(rows) + 1}]"
            rows.append((label, ctx.src_name, ctx.snippet, ctx.score, ctx.date_str))
            seen_snippets.add(ctx.snippet)
            stats["accepted"] += 1
        elif decision == "low_confidence":
            stats["low_confidence"] += 1
            label = None
        elif decision == "duplicate":
            stats["duplicates"] += 1
            label = None
        else:
            stats["empties"] += 1
            label = None

        _debug_filter_message(decision, ctx, reason)

        if diagnostics is not None:
            _append_diagnostic_entry(
                diagnostics,
                hit=ctx.hit,
                status=_status_for_decision(decision),
                reason=reason,
                label=label,
                raw_rank=raw_rank,
                score=ctx.score,
                src_origin=ctx.src_origin,
                src_path=ctx.src_path,
                src_name=ctx.src_name,
                date_str=ctx.date_str,
                include_vectors=include_vectors,
            )

    stats_summary = {
        "accepted": stats["accepted"],
        "low_confidence": stats["low_confidence"],
        "duplicate_or_empty": stats["duplicates"] + stats["empties"],
    }
    return rows, stats_summary


def _log_filter_summary(
    rows: List[Tuple[str, str, str, float, str]],
    *,
    stats: Dict[str, int],
) -> None:
    """Summarise how many snippets survived filtering when DEBUG is enabled."""
    if not DEBUG:
        return
    print(
        "[qa_core] filtering summary: "
        f"{stats['accepted']} accepted, "
        f"{stats['low_confidence']} low-confidence, "
        f"{stats['duplicate_or_empty']} duplicate/empty"
    )
    if not rows:
        return
    print("[qa_core] accepted snippets after filtering:")
    for label, src, snippet, score, _ in rows:
        short = snippet.replace("\n", " ")
        if len(short) > 80:
            short = short[:77] + "..."
        print(f"    {label} from {src} score={score:.3f} text='{short}'")


def collect_relevant_snippets(
    q: str,
    mode: str,
    fund: Optional[str],
    k: int,
    min_confidence: float,
    llm: CompletionsClient,
    extra_docs: Optional[List[str]] = None,
    progress: Optional[Callable[[str], None]] = None,
    *,
    diagnostics: Optional[List[Dict[str, object]]] = None,
    include_vectors: bool = False,
) -> List[Tuple[str, str, str, float, str]]:
    """Return the filtered context snippets shared by both the Responder and conversation flows."""

    q = (q or "").strip()

    if DEBUG:
        print(f"[qa_core] collect_relevant_snippets start: q='{q}', mode={mode}, fund={fund}")

    if not q:
        if DEBUG:
            print("[qa_core] empty question text; skipping vector search")
        if progress:
            progress("Question text is empty; skipping search.")
        return []

    if diagnostics is not None:
        diagnostics.clear()

    if progress:
        progress("Searching knowledge base for relevant snippets...")

    if DEBUG:
        print("[qa_core] searching for context snippets")

    hits = _gather_vector_hits(
        q,
        mode=mode,
        fund=fund,
        k=k,
        include_vectors=include_vectors,
    )
    _extend_with_uploaded_docs(
        hits,
        query=q,
        extra_docs=extra_docs,
        llm=llm,
    )

    if progress:
        progress(f"Found {len(hits)} candidate snippets. Filtering...")

    _log_candidate_hits(hits, k=k)

    rows, stats = _filter_hits(
        hits,
        min_confidence=min_confidence,
        diagnostics=diagnostics,
        include_vectors=include_vectors,
    )
    _log_filter_summary(rows, stats=stats)

    if not rows and progress:
        progress("No relevant information found; skipping language model.")

    return rows


def _build_context_block(rows: List[Tuple[str, str, str, float, str]]) -> str:
    """Flatten accepted snippets into the prompt format consumed by `answer_llm`."""
    block = "\n\n".join(f"{label} {src}: {snippet}" for (label, src, snippet, _, _) in rows)
    if DEBUG:
        print(f"[qa_core] built context with {len(rows)} snippets")
        print("[qa_core] context block:")
        print(block)
    return block


def _length_instruction(length: Optional[str], approx_words: Optional[int]) -> str:
    """Return instruction text honoring explicit word counts over preset labels."""
    if approx_words is not None:
        return f"Please aim for approximately {approx_words} words."
    return PRESET_INSTRUCTIONS.get(length or "medium", "")


def _invoke_llm(prompt: str, llm: CompletionsClient, question: str, attempt: int) -> str:
    """Send the prompt to the configured completions client and normalise the response text."""
    if DEBUG:
        print(f"[qa_core] calling language model (attempt {attempt + 1})")
        print(f"[qa_core] prompt:\n{prompt}")
        print(f"[qa_core] llm type: {type(llm)}")
    raw_response = llm.get_completion(prompt)
    if DEBUG:
        print(f"[qa_core] raw response: {raw_response!r}")
    content = raw_response[0] if isinstance(raw_response, tuple) else raw_response
    answer = (content or "").strip()
    if "summary" not in question.lower():
        idx_summary = answer.lower().rfind("in summary")
        if idx_summary != -1:
            if DEBUG:
                print("[qa_core] stripping trailing 'In summary' section")
            answer = answer[:idx_summary].rstrip()
    return answer


def _extract_citation_order(answer: str) -> List[str]:
    """Return citation tokens in the order they appear (e.g., ['[1]', '[2]', '[3]'])."""
    order: List[str] = []
    for match in CITATION_RE.finditer(answer):
        numbers = [num.strip() for num in match.group(1).split(",")]
        for number in numbers:
            token = f"[{number}]"
            if token not in order:
                order.append(token)
    return order


def _renumber_answer_citations(answer: str, order: List[str]) -> Tuple[str, Dict[str, str]]:
    """Ensure citations are sequential even when the model emits gaps or reorders markers."""
    mapping = {old: f"[{idx + 1}]" for idx, old in enumerate(order)}
    if DEBUG:
        print(f"[qa_core] citation order: {order}")
        print(f"[qa_core] citation mapping: {mapping}")

    def _replace(match: re.Match[str]) -> str:
        numbers = [num.strip() for num in match.group(1).split(",")]
        return "".join(mapping.get(f"[{num}]", f"[{num}]") for num in numbers)

    updated = CITATION_RE.sub(_replace, answer)
    if DEBUG:
        print("[qa_core] renumbered citations")
    return updated, mapping


def _resolve_row_index(
    token: str,
    rows: List[Tuple[str, str, str, float, str]],
    used: Set[int],
) -> Optional[int]:
    """Map a citation token back to a snippet index, falling back to the next unused row."""
    try:
        idx = int(token.strip("[]")) - 1
    except Exception:
        idx = None
        if DEBUG:
            print(f"[qa_core] invalid citation token: {token}")
    if idx is None or not (0 <= idx < len(rows)):
        if DEBUG:
            print(
                f"[qa_core] citation {token} out of range; falling back to next available snippet"
            )
        idx = next((i for i in range(len(rows)) if i not in used), None)
        if idx is None and DEBUG:
            print("[qa_core] no snippets left for fallback")
    return idx


def _build_comments_from_order(
    order: List[str],
    mapping: Dict[str, str],
    rows: List[Tuple[str, str, str, float, str]],
) -> List[Tuple[str, str, str, float, str]]:
    """Translate the ordered citation tokens into the structured comments array."""
    comments: List[Tuple[str, str, str, float, str]] = []
    used_rows: Set[int] = set()
    for token in order:
        idx = _resolve_row_index(token, rows, used_rows)
        if idx is None:
            continue
        used_rows.add(idx)
        label, src, snippet, score, date_str = rows[idx]
        new_label = mapping.get(token, label).strip("[]")
        comments.append((new_label, src, snippet, score, date_str))
        if DEBUG:
            short_snippet = snippet.replace("\n", " ")
            if len(short_snippet) > 60:
                short_snippet = short_snippet[:57] + "..."
            print(
                f"[qa_core] add comment {new_label} from {src} score={score:.3f} snippet='{short_snippet}'"
            )
    return comments


def _generate_answer_with_retries(
    question: str,
    prompt: str,
    llm: CompletionsClient,
    rows: List[Tuple[str, str, str, float, str]],
) -> Tuple[str, List[Tuple[str, str, str, float, str]]]:
    """Call the LLM until citations align with snippets or we exhaust retry attempts."""
    answer = ""
    comments: List[Tuple[str, str, str, float, str]] = []

    for attempt in range(MAX_COMMENT_RETRIES + 1):
        answer = _invoke_llm(prompt, llm, question, attempt)
        order = _extract_citation_order(answer)
        answer, mapping = _renumber_answer_citations(answer, order)
        comments = _build_comments_from_order(order, mapping, rows)
        if DEBUG:
            print(f"[qa_core] built {len(comments)} comments for this attempt")
        if comments or not order or attempt == MAX_COMMENT_RETRIES:
            if not comments and order and attempt != MAX_COMMENT_RETRIES and DEBUG:
                print("[qa_core] zero comments despite citation markers; retrying")
            else:
                break
    return answer, comments


def answer_question(
    q: str,
    mode: str,
    fund: Optional[str],
    k: int,
    length: Optional[str],
    approx_words: Optional[int],
    min_confidence: float,
    llm: CompletionsClient,
    extra_docs: Optional[List[str]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Tuple[str, List[Tuple[str, str, str, float, str]]]:
    """
    Primary entrypoint consumed by `backend.answering.responder` and CLI utilities.

    Return (answer_text, comments) where comments is a list of:
    (new_label_without_brackets, source_name, snippet, score, date_str)

    The answer contains bracket markers like [1], [2], ... which we re-number
    to match the order of comments we return.
    If no search results meet the confidence threshold, the function returns
    "No relevant information found." and an empty comments list without calling
    the language model.
    extra_docs: optional list of document paths to scan with an LLM in addition to vector search.
    """
    if DEBUG:
        print(f"[qa_core] answer_question start: q='{q}', mode={mode}, fund={fund}")
    rows = collect_relevant_snippets(
        q=q,
        mode=mode,
        fund=fund,
        k=k,
        min_confidence=min_confidence,
        llm=llm,
        extra_docs=extra_docs,
        progress=progress,
    )

    if not rows:
        if DEBUG:
            print("[qa_core] no relevant context found; returning fallback answer")
        return "No relevant information found.", []

    ctx_block = _build_context_block(rows)
    if progress:
        progress("Generating answer with language model...")

    length_instr = _length_instruction(length, approx_words)
    prompt = f"{length_instr}\n\n{PROMPTS['answer_llm'].format(context=ctx_block, question=q)}"

    ans, comments = _generate_answer_with_retries(q, prompt, llm, rows)

    if DEBUG:
        print(f"[qa_core] returning answer with {len(comments)} comments")
    if progress:
        progress("Answer generation complete.")
    return ans, comments


# Uncomment the block below to exercise the QA engine without the Streamlit UI.
# It assumes your environment variables point at a running vector search backend
# and that `backend.retrieval` is configured. Adjust the question or fund tag to
# match data available in your dev stack before running:
# `python backend/answering/qa_engine.py`
#
# if __name__ == "__main__":
#     import os
#     from backend.llm.completions_client import CompletionsClient
#
#     client = CompletionsClient(model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
#     sample_question = "What differentiates the Sustainable Growth Fund from peers?"
#     answer, citations = answer_question(
#         q=sample_question,
#         mode=os.getenv("RFP_SEARCH_MODE", "both"),
#         fund=os.getenv("RFP_FUND_TAG"),
#         k=int(os.getenv("RFP_K", "6")),
#         length=os.getenv("RFP_LENGTH"),
#         approx_words=None,
#         min_confidence=float(os.getenv("RFP_MIN_CONFIDENCE", "0.0")),
#         llm=client,
#         extra_docs=None,
#     )
#     print("Answer:", answer)
#     print("Citations:", citations)
