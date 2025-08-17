# my_module.py
import os, re
from typing import Optional, List, Dict
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

def gen_answer(question: str, choices: Optional[List[str]] = None, choice_meta: Optional[List[Dict[str, object]]] = None):
    """Generate an answer. Handles both open and multiple-choice questions."""
    if choices:
        opt_list = "\n".join(f"- {c}" for c in choices)
        select_prompt = f"{question}\nOptions:\n{opt_list}\nSelect the best option and reply with its text only."
        selection, _ = _llm_client.get_completion(select_prompt)
        idx = next((i for i, c in enumerate(choices) if c.strip() == selection.strip()), 0)
        style = "auto"
        if choice_meta:
            markers = "\n".join(f"{i}: {m.get('prefix', '')}" for i, m in enumerate(choice_meta))
            style_prompt = (
                f"You chose '{selection.strip()}'. Option markers:\n{markers}\n"
                "Which marking style fits best? Reply with one word: checkbox, fill, highlight, or auto."
            )
            style_resp, _ = _llm_client.get_completion(style_prompt)
            style = style_resp.strip().lower() or "auto"
        return {"choice_index": idx, "style": style}

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
