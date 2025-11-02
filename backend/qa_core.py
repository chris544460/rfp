#!/usr/bin/env python3
"""
qa_core.py
Home of `answer_question(...)` and its prompt plumbing.

This module centralizes the RAG→LLM answer generation so both the CLI and
other pipelines can reuse it without circular imports.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable, Set

# Your vector search — keep the original import path you already use.
# If your project uses a different path, update this import accordingly.
from .search.vector_search import search
from .llm_doc_search import search_uploaded_docs

# Use the Utilities' client; typically returns (text, usage)
from .answer_composer import CompletionsClient
from .prompts import load_prompts


# Default debug flag; defaults to True unless explicitly disabled via env.
DEBUG = os.getenv("RFP_QA_DEBUG", "1").lower() not in {"", "0", "false"}

# Retry the model if we detect citation markers but end up with no comments.
# Can be overridden via the RFP_COMMENT_RETRIES environment variable.
MAX_COMMENT_RETRIES = int(os.getenv("RFP_COMMENT_RETRIES", "2"))

# Regex for [1] or comma-separated citations like [1, 2]
CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


# ───────────────────────── Prompt loading ─────────────────────────

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
    low_confidence = 0
    duplicate_or_empty = 0

    for idx_raw, hit in enumerate(hits, 1):
        score = float(hit.get("cosine", 0.0))
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

        if score < min_confidence:
            low_confidence += 1
            if DEBUG:
                print(
                    f"[qa_core] filter out id={hit.get('id', 'unknown')} "
                    f"score={score:.3f} < min_confidence {min_confidence}"
                )
            if diagnostics is not None:
                _append_diagnostic_entry(
                    diagnostics,
                    hit=hit,
                    status="filtered_low_confidence",
                    reason=f"score {score:.3f} < min_confidence {min_confidence:.3f}",
                    label=None,
                    raw_rank=idx_raw,
                    score=score,
                    src_origin=src_origin,
                    src_path=src_path,
                    src_name=src_name,
                    date_str=date_str,
                    include_vectors=include_vectors,
                )
            continue

        snippet = (hit.get("text") or "").strip()
        if not snippet or snippet in seen_snippets:
            duplicate_or_empty += 1
            if DEBUG:
                reason = "empty" if not snippet else "duplicate"
                print(
                    f"[qa_core] filter out id={hit.get('id', 'unknown')} {reason} snippet"
                )
            if diagnostics is not None:
                _append_diagnostic_entry(
                    diagnostics,
                    hit=hit,
                    status="filtered_duplicate" if snippet else "filtered_empty",
                    reason="duplicate snippet" if snippet else "empty snippet",
                    label=None,
                    raw_rank=idx_raw,
                    score=score,
                    src_origin=src_origin,
                    src_path=src_path,
                    src_name=src_name,
                    date_str=date_str,
                    include_vectors=include_vectors,
                )
            continue

        label = f"[{len(rows) + 1}]"
        rows.append((label, src_name, snippet, score, date_str))
        seen_snippets.add(snippet)
        if diagnostics is not None:
            _append_diagnostic_entry(
                diagnostics,
                hit=hit,
                status="accepted",
                reason="",
                label=label,
                raw_rank=idx_raw,
                score=score,
                src_origin=src_origin,
                src_path=src_path,
                src_name=src_name,
                date_str=date_str,
                include_vectors=include_vectors,
            )
        if DEBUG:
            print(
                f"[qa_core] accepted snippet {label} from {src_name} score={score:.3f}"
            )

    stats = {
        "accepted": len(rows),
        "low_confidence": low_confidence,
        "duplicate_or_empty": duplicate_or_empty,
    }
    return rows, stats


def _log_filter_summary(
    rows: List[Tuple[str, str, str, float, str]],
    *,
    stats: Dict[str, int],
) -> None:
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
    """Return the filtered context snippets used for answering a question."""

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

    # Build a compact provenance block: "[1] filename: snippet".
    # This format keeps prompts short while still allowing deterministic
    # Word/Excel comments later on.
    ctx_block = "\n\n".join(
        f"{lbl} {src}: {snippet}" for (lbl, src, snippet, _, _) in rows
    )
    if DEBUG:
        print(f"[qa_core] built context with {len(rows)} snippets")
        print("[qa_core] context block:")
        print(ctx_block)
    if progress:
        progress("Generating answer with language model...")

    # 2) Compose the prompt with a length instruction
    if approx_words is not None:
        length_instr = f"Please aim for approximately {approx_words} words."
    else:
        length_instr = PRESET_INSTRUCTIONS.get(length or "medium", "")

    # Inject the length guidance inline so the reusable prompt template stays
    # oblivious to UI-specific knobs (dropdown length vs. explicit word count).
    prompt = f"{length_instr}\n\n{PROMPTS['answer_llm'].format(context=ctx_block, question=q)}"

    ans = ""
    comments: List[Tuple[str, str, str, float, str]] = []
    # The retry loop mitigates the occasional "citations but no comments" bug
    # we observed with some models. We bail early once comments look sane.
    for attempt in range(MAX_COMMENT_RETRIES + 1):
        if DEBUG:
            print(f"[qa_core] calling language model (attempt {attempt + 1})")
            print(f"[qa_core] prompt:\n{prompt}")
            print(f"[qa_core] llm type: {type(llm)}")

        raw_response = llm.get_completion(prompt)
        if DEBUG:
            print(f"[qa_core] raw response: {raw_response!r}")
        if isinstance(raw_response, tuple):
            content = raw_response[0]
        else:
            content = raw_response
        ans = (content or "").strip()

        # Strip a trailing "In summary" section unless the question explicitly
        # requests a summary. Some models tend to append a concluding paragraph
        # beginning with this phrase, which users found redundant.
        if "summary" not in q.lower():
            idx_summary = ans.lower().rfind("in summary")
            if idx_summary != -1:
                if DEBUG:
                    print("[qa_core] stripping trailing 'In summary' section")
                ans = ans[:idx_summary].rstrip()

        # 4) Re-number bracket markers in the answer to reflect the order they first appear
        order: List[str] = []
        for m in CITATION_RE.finditer(ans):
            nums = [n.strip() for n in m.group(1).split(",")]
            for n in nums:
                tok = f"[{n}]"
                if tok not in order:
                    order.append(tok)

        # Some models jumble citation numbers; remap them to a monotonic series
        # so downstream display logic can rely on simple 1..N markers.
        mapping = {old: f"[{i+1}]" for i, old in enumerate(order)}
        if DEBUG:
            print(f"[qa_core] citation order: {order}")
            print(f"[qa_core] citation mapping: {mapping}")

        def _repl(match: re.Match[str]) -> str:
            nums = [n.strip() for n in match.group(1).split(",")]
            return "".join(mapping.get(f"[{n}]", f"[{n}]") for n in nums)

        # Renumbering here keeps answer text and exported comments consistent.
        ans = CITATION_RE.sub(_repl, ans)
        if DEBUG:
            print("[qa_core] renumbered citations")

        # 5) Build comments in that order
        comments: List[Tuple[str, str, str, float, str]] = []
        used_rows: Set[int] = set()
        for old in order:
            idx: Optional[int]
            try:
                idx = int(old.strip("[]")) - 1
            except Exception:
                idx = None
                if DEBUG:
                    print(f"[qa_core] invalid citation token: {old}")
            if idx is None or not (0 <= idx < len(rows)):
                if DEBUG:
                    print(
                        f"[qa_core] citation {old} out of range; falling back to next available snippet"
                    )
                idx = next((i for i in range(len(rows)) if i not in used_rows), None)
                if idx is None:
                    if DEBUG:
                        print("[qa_core] no snippets left for fallback")
                    continue
            used_rows.add(idx)
            lbl, src, snippet, score, date_str = rows[idx]
            new_lbl = mapping.get(old, lbl).strip("[]")
            comments.append((new_lbl, src, snippet, score, date_str))
            if DEBUG:
                short_snippet = snippet.replace("\n", " ")
                if len(short_snippet) > 60:
                    short_snippet = short_snippet[:57] + "..."
                print(
                    f"[qa_core] add comment {new_lbl} from {src} score={score:.3f} snippet='{short_snippet}'"
                )

        if DEBUG:
            print(f"[qa_core] built {len(comments)} comments for this attempt")

        if comments or not order or attempt == MAX_COMMENT_RETRIES:
            break
        if DEBUG:
            print("[qa_core] zero comments despite citation markers; retrying")

    if DEBUG:
        print(f"[qa_core] returning answer with {len(comments)} comments")
    if progress:
        progress("Answer generation complete.")
    return ans, comments
