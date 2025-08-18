# my_module.py (Utilities-owned generator)
import os, re
from typing import Optional, List, Dict

from answer_composer import CompletionsClient
from qa_core import answer_question  # <<— now comes from Utilities core
from prompts import read_prompt

# Defaults (overridable via env vars)
MODEL            = os.getenv("OPENAI_MODEL", "gpt-4o")
SEARCH_MODE      = os.getenv("RFP_SEARCH_MODE", "both")      # "answer"|"question"|"blend"|"dual"|"both"
K                = int(os.getenv("RFP_K", "6"))
FUND_TAG         = os.getenv("RFP_FUND_TAG") or None
MIN_CONFIDENCE   = float(os.getenv("RFP_MIN_CONFIDENCE", "0.0"))
LENGTH_PRESET    = os.getenv("RFP_LENGTH") or "medium"       # "short"|"medium"|"long"
APPROX_WORDS_ENV = os.getenv("RFP_APPROX_WORDS")             # if set, overrides LENGTH
INCLUDE_COMMENTS = os.getenv("RFP_INCLUDE_COMMENTS", "1") == "1"  # "0" to disable

_llm_client = CompletionsClient(model=MODEL)

def _format_with_or_without_comments(ans: str, cmts):
    """Return answer text plus optional citation metadata."""
    if INCLUDE_COMMENTS:
        # cmts: List[(label(str-no-brackets), src, snippet, score, date)]
        citations = {i + 1: c[2] for i, c in enumerate(cmts)}  # map 1→snippet, 2→snippet …
        return {"text": ans, "citations": citations}
    # strip [n] if comments are off
    return re.sub(r"\[\d+\]", "", ans)

def gen_answer(
    question: str,
    choices: Optional[List[str]] = None,
    choice_meta: Optional[List[Dict[str, object]]] = None
):
    """Generate an answer. Handles both open and multiple-choice questions."""
    # Multiple-choice: pick and optionally suggest a marking style
    if choices:
        opt_list = "\n".join(f"- {c}" for c in choices)
        select_template = read_prompt("mc_select")
        select_prompt = select_template.format(question=question, options=opt_list)
        selection, _ = _llm_client.get_completion(select_prompt)
        idx = next((i for i, c in enumerate(choices) if c.strip() == selection.strip()), 0)
        style = "auto"
        if choice_meta:
            markers = "\n".join(f"{i}: {m.get('prefix', '')}" for i, m in enumerate(choice_meta))
            style_template = read_prompt("mc_style")
            style_prompt = style_template.format(selection=selection.strip(), markers=markers)
            style_resp, _ = _llm_client.get_completion(style_prompt)
            style = (style_resp or "").strip().lower() or "auto"
        citations = {}
        if INCLUDE_COMMENTS:
            # Re-use core QA to fetch supporting snippets for comment context
            ans, cmts = answer_question(
                question,
                SEARCH_MODE,
                FUND_TAG,
                K,
                None,
                None,
                MIN_CONFIDENCE,
                _llm_client,
            )
            citations = {i + 1: c[2] for i, c in enumerate(cmts)}
        result = {"choice_index": idx, "style": style}
        if citations:
            result["citations"] = citations
        return result

    # Free-text: call core QA
    approx_words: Optional[int] = int(APPROX_WORDS_ENV) if APPROX_WORDS_ENV else None
    length = None if approx_words is not None else LENGTH_PRESET

    ans, cmts = answer_question(
        question,
        SEARCH_MODE,
        FUND_TAG,
        K,
        length,
        approx_words,
        MIN_CONFIDENCE,
        _llm_client,
    )
    return _format_with_or_without_comments(ans, cmts)
