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
from typing import Dict, List, Optional, Tuple, Callable

# Your vector search — keep the original import path you already use.
# If your project uses a different path, update this import accordingly.
from search.vector_search import search
from llm_doc_search import search_uploaded_docs

# Use the Utilities' client; typically returns (text, usage)
from answer_composer import CompletionsClient
from prompts import load_prompts


# Default debug flag; always on unless caller toggles.
DEBUG = True

# Retry the model if we detect citation markers but end up with no comments.
# Can be overridden via the RFP_COMMENT_RETRIES environment variable.
MAX_COMMENT_RETRIES = int(os.getenv("RFP_COMMENT_RETRIES", "2"))

# Regex for [1] or comma-separated citations like [1, 2]
CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

# Model used to verify snippet relevance. Can be overridden via
# the RFP_RELEVANCE_MODEL environment variable.
RELEVANCE_MODEL = os.getenv("RFP_RELEVANCE_MODEL", "gpt-4.1")


# ───────────────────────── Prompt loading ─────────────────────────

PROMPTS = load_prompts(
    {
        name: ""
        for name in (
            "extract_questions",
            "answer_search_context",
            "answer_llm",
            "relevance_filter",
        )
    }
)

PRESET_INSTRUCTIONS: Dict[str, str] = {
    "short": "Answer briefly in 1–2 sentences.",
    "medium": "Answer in one concise paragraph.",
    "long": "Answer in detail (up to one page).",
    "auto": "Answer using only the provided sources and choose an appropriate length.",
}


# ───────────────────────── Core answering ─────────────────────────


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
    if progress:
        progress("Searching knowledge base for relevant snippets...")
    # 1) Retrieve candidate context snippets
    if DEBUG:
        print("[qa_core] searching for context snippets")
    if mode == "both":
        # Back-compat: treat "both" as blend + dual
        hits = search(q, k=k, mode="blend", fund_filter=fund) + search(
            q, k=k, mode="dual", fund_filter=fund
        )
    else:
        hits = search(q, k=k, mode=mode, fund_filter=fund)
    if extra_docs:
        if DEBUG:
            print(f"[qa_core] LLM searching {len(extra_docs)} uploaded docs")
        hits.extend(search_uploaded_docs(q, extra_docs, llm))
    if progress:
        progress(f"Found {len(hits)} candidate snippets. Filtering...")
    if DEBUG:
        print(f"[qa_core] retrieved {len(hits)} hits before filtering")
        top_n = min(len(hits), k)
        print(f"[qa_core] top {top_n} hits:")
        for i, h in enumerate(hits[:top_n], 1):
            meta = h.get("meta", {}) or {}
            src = meta.get("source", "unknown")
            doc_id = h.get("id", "unknown")
            score = float(h.get("cosine", 0.0))
            snippet = (h.get("text") or "").strip().replace("\n", " ")
            if len(snippet) > 80:
                snippet = snippet[:77] + "..."
            print(
                f"    {i}. id={doc_id} score={score:.3f} source={src} text='{snippet}'"
            )

    relevance_llm = CompletionsClient(model=RELEVANCE_MODEL) if RELEVANCE_MODEL else None
    if DEBUG and relevance_llm:
        print(f"[qa_core] using relevance model: {RELEVANCE_MODEL}")

    seen_snippets = set()
    rows: List[Tuple[str, str, str, float, str]] = (
        []
    )  # (lbl, src, snippet, score, date)
    low_confidence = 0
    duplicate_or_empty = 0
    irrelevant = 0
    for h in hits:
        score = float(h.get("cosine", 0.0))
        doc_id = h.get("id", "unknown")
        if score < min_confidence:
            low_confidence += 1
            if DEBUG:
                print(
                    f"[qa_core] filter out id={doc_id} score={score:.3f} < min_confidence {min_confidence}"
                )
            continue
        txt = (h.get("text") or "").strip()
        if not txt or txt in seen_snippets:
            duplicate_or_empty += 1
            if DEBUG:
                reason = "empty" if not txt else "duplicate"
                print(f"[qa_core] filter out id={doc_id} {reason} snippet")
            continue
        if relevance_llm:
            rel_prompt = PROMPTS["relevance_filter"].format(question=q, snippet=txt)
            if DEBUG:
                print(f"[qa_core] LLM relevance check for id={doc_id}")
                print(f"[qa_core] relevance prompt:\n{rel_prompt}")
            rel_raw = relevance_llm.get_completion(rel_prompt)
            rel_text = (
                rel_raw[0] if isinstance(rel_raw, tuple) else rel_raw
            ).strip().lower()
            if DEBUG:
                print(f"[qa_core] relevance model output: {rel_text!r}")
            if not rel_text.startswith("t"):
                irrelevant += 1
                if DEBUG:
                    print(
                        f"[qa_core] filter out id={doc_id} marked irrelevant by relevance model"
                    )
                continue
        meta = h.get("meta", {}) or {}
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

        lbl = f"[{len(rows)+1}]"
        rows.append((lbl, src_name, txt, score, date_str))
        seen_snippets.add(txt)
        if DEBUG:
            print(f"[qa_core] accepted snippet {lbl} from {src_name} score={score:.3f}")

    if DEBUG:
        print(
            f"[qa_core] filtering summary: {len(rows)} accepted, {low_confidence} low-confidence, {duplicate_or_empty} duplicate/empty, {irrelevant} irrelevant"
        )
        if rows:
            print("[qa_core] accepted snippets after filtering:")
            for lbl, src, snippet, score, _ in rows:
                short = snippet.replace("\n", " ")
                if len(short) > 80:
                    short = short[:77] + "..."
                print(
                    f"    {lbl} from {src} score={score:.3f} text='{short}'"
                )

    if not rows:
        if progress:
            progress("No relevant information found; skipping language model.")
        if DEBUG:
            print("[qa_core] no relevant context found; returning fallback answer")
        return "No relevant information found.", []

    # Build the context block presented to the model
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

    prompt = f"{length_instr}\n\n{PROMPTS['answer_llm'].format(context=ctx_block, question=q)}"

    ans = ""
    comments: List[Tuple[str, str, str, float, str]] = []
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

        mapping = {old: f"[{i+1}]" for i, old in enumerate(order)}
        if DEBUG:
            print(f"[qa_core] citation order: {order}")
            print(f"[qa_core] citation mapping: {mapping}")

        def _repl(match: re.Match[str]) -> str:
            nums = [n.strip() for n in match.group(1).split(",")]
            return "".join(mapping.get(f"[{n}]", f"[{n}]") for n in nums)

        ans = CITATION_RE.sub(_repl, ans)
        if DEBUG:
            print("[qa_core] renumbered citations")

        # 5) Build comments in that order
        comments = []
        for old in order:
            try:
                idx = int(old.strip("[]")) - 1
            except Exception:
                if DEBUG:
                    print(f"[qa_core] invalid citation token: {old}")
                continue
            if 0 <= idx < len(rows):
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
            else:
                if DEBUG:
                    print(
                        f"[qa_core] citation {old} out of range for {len(rows)} snippets"
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
