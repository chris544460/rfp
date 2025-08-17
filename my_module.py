# my_module.py
import os, re
from typing import Optional
from answer.answer_composer import CompletionsClient
from cli_app import answer_question  # you already import this in the notebook

# Defaults (overridable via env vars)
MODEL            = os.getenv("OPENAI_MODEL", "gpt-4o")
SEARCH_MODE      = os.getenv("RFP_SEARCH_MODE", "dual")      # "answer"|"question"|"blend"|"dual"
K                = int(os.getenv("RFP_K", "6"))
FUND_TAG         = os.getenv("RFP_FUND_TAG") or None
MIN_CONFIDENCE   = float(os.getenv("RFP_MIN_CONFIDENCE", "0.0"))
LENGTH_PRESET    = os.getenv("RFP_LENGTH") or "medium"       # "short"|"medium"|"long"
APPROX_WORDS_ENV = os.getenv("RFP_APPROX_WORDS")             # if set, overrides LENGTH
INCLUDE_COMMENTS = os.getenv("RFP_INCLUDE_COMMENTS", "0") == "1"

_llm_client = CompletionsClient(model=MODEL)

def _format_with_or_without_comments(ans: str, cmts) -> str:
    if INCLUDE_COMMENTS:
        lines = [ans, "", "Sources:"]
        for i, (lbl, src, snippet, score, date_str) in enumerate(cmts):
            lines.append(f"[{i+1}] {lbl} — {src} ({date_str}, score {score:.3f})")
        return "\n".join(lines)
    # strip bracket markers like [1] if comments are off
    return re.sub(r"\[\d+\]", "", ans)

def gen_answer(question: str) -> str:
    """Generate an answer using the same RAG path as the Streamlit app."""
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
    # The DOCX applier writes plain text; keep Markdown if you like, or strip markers here.
    return _format_with_or_without_comments(ans, cmts)
