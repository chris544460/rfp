# my_module.py (Utilities-owned generator)
import os, re, json
from typing import Optional, List, Dict

from answer_composer import CompletionsClient
from qa_core import answer_question  # <<— now comes from Utilities core
from prompts import read_prompt

# Defaults (overridable via env vars)
MODEL            = os.getenv("OPENAI_MODEL", "gpt-4.1-nano-2025-04-14_research")
SEARCH_MODE      = os.getenv("RFP_SEARCH_MODE", "both")      # "answer"|"question"|"blend"|"dual"|"both"
K                = int(os.getenv("RFP_K", "6"))
FUND_TAG         = os.getenv("RFP_FUND_TAG") or None
MIN_CONFIDENCE   = float(os.getenv("RFP_MIN_CONFIDENCE", "0.0"))
LENGTH_PRESET    = os.getenv("RFP_LENGTH") or "medium"       # "short"|"medium"|"long"
APPROX_WORDS_ENV = os.getenv("RFP_APPROX_WORDS")             # if set, overrides LENGTH
INCLUDE_COMMENTS = os.getenv("RFP_INCLUDE_COMMENTS", "1") == "1"  # "0" to disable

_llm_client = CompletionsClient(model=MODEL)

# Maintain history of previous questions so follow-ups can pull context.
QUESTION_HISTORY: List[str] = []

# Prompt template for deciding whether a question depends on prior ones.
FOLLOWUP_PROMPT = read_prompt(
    "followup_detect",
    (
        "Given a current question and a list of previous questions, "
        "return JSON with keys 'follow_up' (true/false) and 'indices' (list of "
        "integers of prior questions that provide necessary context)."
    ),
)

DEBUG = True

def _format_with_or_without_comments(ans: str, cmts):
    """Return answer text plus optional citation metadata."""
    if INCLUDE_COMMENTS:
        # cmts: List[(label(str-no-brackets), src, snippet, score, date)]
        citations = {
            i + 1: {"text": c[2], "source_file": c[1]} for i, c in enumerate(cmts)
        }
        return {"text": ans, "citations": citations}
    # strip [n] if comments are off
    return re.sub(r"\[\d+\]", "", ans)


def _format_mc_answer(ans: str, choices: List[str]) -> str:
    """Rewrite LLM output so choices are named up front.

    The language model is expected to return JSON of the form::

        {"correct": ["A", ...], "explanations": {"A": "why", ...}}

    This function parses that structure and converts it into a human-readable
    sentence such as ``"The correct answers are: Option1, Option2."`` followed
    by per-option explanations (e.g. ``"A. because..."``). If JSON parsing
    fails, it falls back to a best-effort regex that looks for leading option
    letters in the raw text.
    """

    try:
        data = json.loads(ans)
        letters = [str(l).strip().upper() for l in data.get("correct", [])]
        if letters:
            explanations: Dict[str, str] = data.get("explanations", {}) or {}
            names: List[str] = []
            details: List[str] = []
            for l in letters:
                idx = ord(l) - ord("A")
                if 0 <= idx < len(choices):
                    names.append(choices[idx])
                    expl = explanations.get(l, "").strip()
                    if expl:
                        details.append(f"{l}. {expl}")
            if names:
                intro = (
                    f"The correct answer is: {names[0]}."
                    if len(names) == 1
                    else "The correct answers are: " + ", ".join(names) + "."
                )
                tail = " ".join(details)
                return f"{intro} {tail}".strip()
    except Exception:
        pass

    # Fallback: look for leading letters in free-form text
    match = re.match(r"([A-Z](?:\s*,\s*[A-Z])*)[\.\)]\s*", ans)
    if not match:
        return ans

    letters = [l.strip() for l in match.group(1).split(",")]
    explanation = ans[match.end():].lstrip()

    names: List[str] = []
    for l in letters:
        idx = ord(l) - ord("A")
        if 0 <= idx < len(choices):
            names.append(choices[idx])

    if not names:
        return ans

    if len(names) == 1:
        intro = f"The correct answer is: {names[0]}."
    else:
        intro = "The correct answers are: " + ", ".join(names) + "."

    return f"{intro} {explanation}" if explanation else intro

def _detect_followup(question: str, history: List[str]) -> List[int]:
    """Use the LLM to determine which previous questions provide context."""
    if not history:
        return []
    history_block = "\n".join(f"{i+1}. {q}" for i, q in enumerate(history))
    prompt = FOLLOWUP_PROMPT.format(question=question, history=history_block)
    if DEBUG:
        print("[my_module] checking if question is follow-up")
    try:
        content, _ = _llm_client.get_completion(prompt)
        data = json.loads(content or "{}")
    except Exception:
        return []
    if not isinstance(data, dict) or not data.get("follow_up"):
        return []
    indices = []
    for i in data.get("indices", []):
        try:
            idx = int(i)
            if 1 <= idx <= len(history):
                indices.append(idx)
        except Exception:
            continue
    if DEBUG and indices:
        ctx = " | ".join(history[i - 1] for i in indices)
        print(
            f"[my_module] follow-up detected; using context from questions {indices}: {ctx}"
        )
    return indices

def gen_answer(
    question: str,
    choices: Optional[List[str]] = None,
    choice_meta: Optional[List[Dict[str, object]]] = None
):
    """Generate an answer. Handles both open and multiple-choice questions."""
    # Determine if this question relies on previous ones
    indices = _detect_followup(question, QUESTION_HISTORY)
    question_with_ctx = question
    if indices:
        ctx_text = " ".join(QUESTION_HISTORY[i - 1] for i in indices)
        question_with_ctx = f"{ctx_text}\n\n{question}"

    # Multiple-choice: provide textual explanation with citations
    if choices:
        opt_lines = "\n".join(f"{chr(65+i)}. {c}" for i, c in enumerate(choices))
        mc_question = (
            f"{question_with_ctx}\n\nOptions:\n{opt_lines}\n\n"
            "Identify the correct option letter(s). For each correct option, provide a brief explanation "
            "with citations in square brackets like [1]. Return the result as JSON with keys "
            "'correct' (list of letters) and 'explanations' (mapping letters to explanations)."
        )
        ans, cmts = answer_question(
            mc_question,
            SEARCH_MODE,
            FUND_TAG,
            K,
            None,
            None,
            MIN_CONFIDENCE,
            _llm_client,
        )
        ans = _format_mc_answer(ans, choices)
        QUESTION_HISTORY.append(question)
        return _format_with_or_without_comments(ans, cmts)

    # Free-text: call core QA
    approx_words: Optional[int] = int(APPROX_WORDS_ENV) if APPROX_WORDS_ENV else None
    length = None if approx_words is not None else LENGTH_PRESET

    ans, cmts = answer_question(
        question_with_ctx,
        SEARCH_MODE,
        FUND_TAG,
        K,
        length,
        approx_words,
        MIN_CONFIDENCE,
        _llm_client,
    )
    QUESTION_HISTORY.append(question)
    return _format_with_or_without_comments(ans, cmts)
