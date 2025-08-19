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
from typing import Dict, List, Optional, Tuple

# Your vector search — keep the original import path you already use.
# If your project uses a different path, update this import accordingly.
from search.vector_search import search

# Use the Utilities' client so the return type is (text, usage)
from answer_composer import CompletionsClient
from prompts import load_prompts


# Default debug flag; always on unless caller toggles.
DEBUG = True


# ───────────────────────── Prompt loading ─────────────────────────

PROMPTS = load_prompts({name: "" for name in ("extract_questions", "answer_search_context", "answer_llm")})

PRESET_INSTRUCTIONS: Dict[str, str] = {
    "short": "Answer briefly in 1–2 sentences.",
    "medium": "Answer in one concise paragraph.",
    "long": "Answer in detail (up to one page).",
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
) -> Tuple[str, List[Tuple[str, str, str, float, str]]]:
    """
    Return (answer_text, comments) where comments is a list of:
    (new_label_without_brackets, source_name, snippet, score, date_str)

    The answer contains bracket markers like [1], [2], ... which we re-number
    to match the order of comments we return.
    If no search results meet the confidence threshold, the function returns
    "No relevant information found." and an empty comments list without calling
    the language model.
    """
    if DEBUG:
        print(f"[qa_core] answer_question start: q='{q}', mode={mode}, fund={fund}")
    # 1) Retrieve candidate context snippets
    if DEBUG:
        print("[qa_core] searching for context snippets")
    if mode == "both":
        # Back-compat: treat "both" as blend + dual
        hits = search(q, k=k, mode="blend", fund_filter=fund) + search(q, k=k, mode="dual", fund_filter=fund)
    else:
        hits = search(q, k=k, mode=mode, fund_filter=fund)
    if DEBUG:
        print(f"[qa_core] retrieved {len(hits)} hits")
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

    seen_snippets = set()
    rows: List[Tuple[str, str, str, float, str]] = []  # (lbl, src, snippet, score, date)
    for h in hits:
        score = float(h.get("cosine", 0.0))
        if score < min_confidence:
            if DEBUG:
                print(f"[qa_core] skip hit below confidence {score:.3f}")
            continue
        txt = (h.get("text") or "").strip()
        if not txt or txt in seen_snippets:
            if DEBUG:
                print("[qa_core] skip empty/duplicate snippet")
            continue
        meta = h.get("meta", {}) or {}
        src_path = str(meta.get("source", "")) or "unknown"
        src_name = Path(src_path).name if src_path else "unknown"
        try:
            mtime = Path(src_path).stat().st_mtime if src_path and Path(src_path).exists() else None
            date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") if mtime else "unknown"
        except Exception:
            date_str = "unknown"

        lbl = f"[{len(rows)+1}]"
        rows.append((lbl, src_name, txt, score, date_str))
        seen_snippets.add(txt)
        if DEBUG:
            print(f"[qa_core] accepted snippet {lbl} from {src_name} score={score:.3f}")

    if not rows:
        if DEBUG:
            print("[qa_core] no relevant context found; returning fallback answer")
        return "No relevant information found.", []

    # Build the context block presented to the model
    ctx_block = "\n\n".join(f"{lbl} {src}: {snippet}" for (lbl, src, snippet, _, _) in rows)
    if DEBUG:
        print(f"[qa_core] built context with {len(rows)} snippets")

    # 2) Compose the prompt with a length instruction
    if approx_words is not None:
        length_instr = f"Please aim for approximately {approx_words} words."
    else:
        length_instr = PRESET_INSTRUCTIONS.get(length or "medium", "")

    prompt = f"{length_instr}\n\n{PROMPTS['answer_llm'].format(context=ctx_block, question=q)}"

    # 3) Call the model
    if DEBUG:
        print("[qa_core] calling language model")
        print(f"[qa_core] prompt:\n{prompt}")
        print(f"[qa_core] llm type: {type(llm)}")

    raw_response = llm.get_completion(prompt)
    if DEBUG:
        print(f"[qa_core] raw response: {raw_response!r}")
    try:
        content, _usage = raw_response
    except Exception as e:
        if DEBUG:
            print(f"[qa_core] error unpacking response: {e}")
        raise
    ans = (content or "").strip()

    # 4) Re-number bracket markers [n] in the answer to reflect the order they first appear
    order: List[str] = []
    for m in re.finditer(r"\[(\d+)\]", ans):
        tok = m.group(0)  # like "[3]"
        if tok not in order:
            order.append(tok)

    mapping = {old: f"[{i+1}]" for i, old in enumerate(order)}
    for old, new in mapping.items():
        ans = ans.replace(old, new)
    if DEBUG:
        print("[qa_core] renumbered citations")

    # 5) Build comments in that order
    comments: List[Tuple[str, str, str, float, str]] = []
    for old in order:
        try:
            idx = int(old.strip("[]")) - 1
        except Exception:
            continue
        if 0 <= idx < len(rows):
            lbl, src, snippet, score, date_str = rows[idx]
            new_lbl = mapping.get(old, lbl).strip("[]")  # "1", "2", ...
            comments.append((new_lbl, src, snippet, score, date_str))

    if DEBUG:
        print(f"[qa_core] returning answer with {len(comments)} comments")
    return ans, comments
